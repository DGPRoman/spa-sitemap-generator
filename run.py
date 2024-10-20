import argparse
import time
from app.parser import Parser
from app.logger import logger


class CommandHandler:
    def __init__(self):
        self.parser = argparse.ArgumentParser(description='CLI utility')
        self.subparsers = self.parser.add_subparsers(title='commands', dest='command', required=True)
        self._register_commands()

    def _register_commands(self):
        commands = {
            'new': {
                'help': 'Delete old data from the database and perform a clean parsing',
                'handler': self.handle_new,
            },
            'update': {
                'help': 'Continue an existing parsing process',
                'handler': self.handle_update,
            },
            'export': {
                'help': 'Export data to a file',
                'handler': self.handle_export,
            }
        }

        for command, details in commands.items():
            subparser = self.subparsers.add_parser(command, help=details['help'])
            subparser.set_defaults(func=details['handler'])

    @staticmethod
    def handle_new():
        logger.info("Starting new parsing process.")
        Parser().main()

    @staticmethod
    def handle_update():
        logger.info("Updating existing parsing process.")
        Parser().update()

    @staticmethod
    def handle_export():
        logger.info("Exporting data.")
        Parser().export()

    def execute(self):
        args = self.parser.parse_args()
        args.func()


def measure_execution_time(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        logger.info(f"Finished in {round(end_time - start_time, 2)} seconds")
        return result
    return wrapper


@measure_execution_time
def main():
    handler = CommandHandler()
    handler.execute()


if __name__ == '__main__':
    main()
