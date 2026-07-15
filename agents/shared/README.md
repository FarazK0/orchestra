# Shared agent infrastructure

- llm.py    the single LLM client wrapper. All provider calls go through here.
            Records tokens + cost per call to the control plane.
- loop.py   base agent loop: receive context package -> plan -> act via gateway
            -> self-check against acceptance criteria -> commit to branch.
