import json
import time
from app.db import DatabaseManager
from app.export import Export
from app.logger import logger
from contextlib import contextmanager
from selenium import webdriver
from selenium.webdriver.common.by import By


class ConfigLoader:
    CONFIG_FILE = 'config.json'

    @staticmethod
    def load_config() -> dict:
        try:
            with open(ConfigLoader.CONFIG_FILE, 'r') as file:
                return json.load(file)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            logger.error(f"Error loading config: {e}")
            return {}


@contextmanager
def create_browser(width: int, height: int):
    driver = webdriver.Chrome()
    try:
        driver.set_window_size(width, height)
        yield driver
    finally:
        driver.quit()


class LinkParser:
    def __init__(self, config: dict):
        self.configs = config
        self.next_page_url = None

    def search_links_on_page(self):
        with create_browser(Parser.WINDOW_WIDTH, Parser.WINDOW_HEIGHT) as chrome_driver:
            self._process_pages(chrome_driver)

    def _process_pages(self, chrome_driver):
        while self.next_page_url:
            logger.info(f"Processing page: {self.next_page_url}")
            chrome_driver.get(self.next_page_url)
            time.sleep(self.configs.get("delay", 1))
            links = chrome_driver.find_elements(By.TAG_NAME, 'a')
            if links:
                self._save_links(links)
            else:
                logger.warning(f"No links found on page: {self.next_page_url}")

            DatabaseManager().save_page_status(self.next_page_url, 1)
            self.next_page_url = DatabaseManager().get_next_page()

    def _save_links(self, links):
        new_links = [link.get_attribute('href') for link in links if self._is_valid_link(link)]
        DatabaseManager().save_links(new_links)

    def _is_valid_link(self, link) -> bool:
        href = link.get_attribute('href')
        return href and href.startswith(self.configs["url"]) and '#' not in href


class Parser:
    WINDOW_WIDTH = 1440
    WINDOW_HEIGHT = 980

    def __init__(self):
        self.configs = ConfigLoader.load_config()
        self.link_parser = LinkParser(self.configs)

    def update(self):
        logger.info('Update process started.')
        self.link_parser.next_page_url = DatabaseManager().get_next_page()
        self.link_parser.search_links_on_page()

    def main(self):
        logger.info('Main process started.')
        self._setup_database()
        self.link_parser.next_page_url = self.configs.get("url")
        self.link_parser.search_links_on_page()

    @staticmethod
    def export():
        logger.info('Export process started.')
        exporter = Export('sitemap.xml')
        exporter.generate_sitemap()

    @staticmethod
    def _setup_database():
        db_manager = DatabaseManager()
        db_manager.drop_tables()
        db_manager.create_tables()


if __name__ == '__main__':
    logger.info('Main process started.')
    parser = Parser()
    parser.main()  # Start the parsing process
