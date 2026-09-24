"""A throwaway Postgres cluster for the public results SQL tests.

The tests that prove row level security need a real Postgres. This helper finds
the server binaries (PATH, then the versioned install directories Homebrew and
Debian use), starts a private cluster in a short temporary directory that
listens on a unix socket only, and stops and deletes it afterwards. It never
opens a TCP port. When no binaries are found the tests skip, and they say so.

GitHub's Ubuntu runners ship PostgreSQL with the service disabled, so the gate
finds the binaries under /usr/lib/postgresql/<major>/bin and runs these tests.

``SUPABASE_BOOTSTRAP`` approximates what Supabase provisions before any
migration runs, taken from the initial schema in supabase/postgres. It creates
the three API roles and, importantly, the default privileges that give them
every privilege on new tables in ``public``. Without those grants the
migration's revokes would be tested against nothing. It approximates
Supabase and leaves out everything else a hosted project provisions.
PostgREST's role switch is modelled with ``set role``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SUPABASE_ROLES = """
create role anon nologin noinherit;
create role authenticated nologin noinherit;
create role service_role nologin noinherit bypassrls;
"""

SUPABASE_BOOTSTRAP = """
grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on functions to anon, authenticated, service_role;
"""

_TOOLS = ("initdb", "pg_ctl", "psql")

# The macOS server refuses to start when the inherited locale is unset or
# unusable ("postmaster became multithreaded during startup"), so every tool
# runs under the C locale.
_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


def _version_of(bin_dir: Path) -> tuple[int, ...]:
    out = subprocess.run(
        [str(bin_dir / "postgres"), "--version"], capture_output=True, text=True, check=False
    ).stdout
    match = re.search(r"(\d+)(?:\.(\d+))?", out)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.groups() if part is not None)


def find_postgres_bin() -> Path | None:
    """The newest directory holding initdb, pg_ctl, psql and postgres, if any."""
    candidates: list[Path] = []
    override = os.environ.get("TRACE_TEST_PG_BIN")
    if override:
        candidates.append(Path(override))
    on_path = shutil.which("initdb")
    if on_path:
        candidates.append(Path(on_path).resolve().parent)
    for pattern in (
        "/usr/lib/postgresql/*/bin",
        "/opt/homebrew/opt/postgresql@*/bin",
        "/usr/local/opt/postgresql@*/bin",
    ):
        candidates.extend(Path(p) for p in sorted(Path("/").glob(pattern.lstrip("/"))))
    usable = [c for c in candidates if all((c / tool).is_file() for tool in (*_TOOLS, "postgres"))]
    if not usable:
        return None
    if override:
        return usable[0]
    return max(usable, key=_version_of)


@dataclass
class PsqlResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class PgCluster:
    """One private cluster. ``psql`` runs SQL as the superuser ``postgres``."""

    def __init__(self, bin_dir: Path):
        self.bin_dir = bin_dir
        # Unix socket paths are limited to about a hundred bytes, and pytest's
        # temporary directories on macOS are longer than that.
        self.root = Path(tempfile.mkdtemp(prefix="trpg-", dir="/tmp"))
        self.data = self.root / "data"
        self.version = _version_of(bin_dir)
        self._databases = 0

    def start(self) -> None:
        self._run(
            "initdb",
            "-D",
            str(self.data),
            "-U",
            "postgres",
            "--auth=trust",
            "--encoding=UTF8",
            "--no-locale",
        )
        options = f"-k {self.root} -c listen_addresses='' -F"
        self._run(
            "pg_ctl",
            "-D",
            str(self.data),
            "-o",
            options,
            "-l",
            str(self.root / "log"),
            "-w",
            "start",
        )
        result = self.psql(SUPABASE_ROLES, database="postgres")
        if not result.ok:
            raise RuntimeError(f"could not create the Supabase roles: {result.stderr}")

    def stop(self) -> None:
        if self.data.exists():
            subprocess.run(
                [str(self.bin_dir / "pg_ctl"), "-D", str(self.data), "-m", "immediate", "stop"],
                capture_output=True,
                check=False,
                env=_ENV,
            )
        shutil.rmtree(self.root, ignore_errors=True)

    def new_database(self) -> str:
        """A fresh database with the Supabase bootstrap applied, no migrations."""
        self._databases += 1
        name = f"results_{self._databases}"
        created = self.psql(f"create database {name};", database="postgres")
        if not created.ok:
            raise RuntimeError(created.stderr)
        boot = self.psql(SUPABASE_BOOTSTRAP, database=name)
        if not boot.ok:
            raise RuntimeError(boot.stderr)
        return name

    def copy_database(self, template: str) -> str:
        """A new database copied from ``template``, which must have no connections."""
        self._databases += 1
        name = f"results_{self._databases}"
        created = self.psql(f"create database {name} template {template};", database="postgres")
        if not created.ok:
            raise RuntimeError(created.stderr)
        return name

    def psql(self, sql: str, *, database: str, tuples_only: bool = False) -> PsqlResult:
        args = [
            str(self.bin_dir / "psql"),
            "-X",
            "-h",
            str(self.root),
            "-U",
            "postgres",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ]
        if tuples_only:
            args[1:1] = ["-A", "-t", "-q"]
        done = subprocess.run(
            args, input=sql, capture_output=True, text=True, check=False, env=_ENV
        )
        return PsqlResult(done.returncode, done.stdout, done.stderr)

    def apply_file(self, path: Path, *, database: str) -> PsqlResult:
        args = [
            str(self.bin_dir / "psql"),
            "-X",
            "-q",
            "-h",
            str(self.root),
            "-U",
            "postgres",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "--single-transaction",
            "-f",
            str(path),
        ]
        done = subprocess.run(args, capture_output=True, text=True, check=False, env=_ENV)
        return PsqlResult(done.returncode, done.stdout, done.stderr)

    def _run(self, tool: str, *args: str) -> None:
        done = subprocess.run(
            [str(self.bin_dir / tool), *args], capture_output=True, text=True, check=False, env=_ENV
        )
        if done.returncode != 0:
            raise RuntimeError(f"{tool} failed: {done.stderr or done.stdout}")


def dollar_quote(text: str) -> str:
    """Quote ``text`` as a Postgres dollar-quoted literal with an unused tag."""
    n = 0
    while f"$q{n}$" in text:
        n += 1
    return f"$q{n}${text}$q{n}$"
