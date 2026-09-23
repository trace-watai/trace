"""Reference outside agents: working examples of the TargetAgent contract.

``langgraph_ref`` wraps a LangGraph graph and ``openai_agents_ref`` wraps an
OpenAI Agents SDK agent. Each needs its extra (``pip install -e ".[langgraph]"``
or ``".[openai-agents]"``); nothing in the core package imports them, so the
harness and its tests run with neither installed.

The model underneath each reference agent is scripted. Its turns come from a
task's fixture script or from a cassette recorded from that script (see
``turns.py``), so a reference run shows the outside-agent path working end to
end and says nothing about how a live model behaves.
"""
