"""V2 entry point; schema/data import is explicit and never runs at startup.

Legacy handlers remain for reference but are not imported or registered here.
"""

from app.v2.runtime import entrypoint, main as main


if __name__ == "__main__":
    entrypoint()
