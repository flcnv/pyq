import asyncio
import sys

from src.worker.main import main

sys.exit(asyncio.run(main()))
