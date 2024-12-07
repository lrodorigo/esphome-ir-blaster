import asyncio
import logging
import time
from sys import argv

from trv_controller import trv_controller

logging.basicConfig(level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')

if __name__ == '__main__':
    config_file = argv[1] if len(argv) > 1 else "./config.yaml"

    asyncio.run(
        trv_controller.run(config_file)
    )