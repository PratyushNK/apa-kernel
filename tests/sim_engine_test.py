
import os
import sys
import asyncio

# Ensure project root is on sys.path so running this file directly works
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Also add the simulator package directory so its internal unqualified imports resolve
sim_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "simulator"))
if sim_dir not in sys.path:
    sys.path.insert(0, sim_dir)

from simulator.runner import simulation_runner
from kernel.engine.runner import engine_runner

async def main() -> None:
    sim = asyncio.create_task(simulation_runner())
    await asyncio.sleep(1)
    eng = asyncio.create_task(engine_runner())
    await asyncio.sleep(55)

if __name__ == "__main__":
    asyncio.run(main())