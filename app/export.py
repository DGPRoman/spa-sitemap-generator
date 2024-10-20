from xml.etree.ElementTree import Element, SubElement, ElementTree
from app.db import DatabaseManager
import xml.etree.ElementTree as ET
from xml.dom.minidom import parseString
from app.logger import logger


class Export:
    def __init__(self, output_path='sitemap.xml'):
        self.output_path = output_path

    def generate_sitemap(self):
        with DatabaseManager() as db_manager:
            urls = self.fetch_urls(db_manager)
            self.create_sitemap(urls)

    @staticmethod
    def fetch_urls(db_manager):
        query = "SELECT url FROM pages WHERE status = 1"
        db_manager.cursor.execute(query)
        urls = [row[0] for row in db_manager.cursor.fetchall()]
        return urls

    def create_sitemap(self, urls):
        urlset = Element('urlset', xmlns='http://www.sitemaps.org/schemas/sitemap/0.9')

        for url in urls:
            url_element = SubElement(urlset, 'url')
            loc = SubElement(url_element, 'loc')
            loc.text = url

        tree = ElementTree(urlset)
        self.write_sitemap(tree)

    def write_sitemap(self, tree):
        try:
            raw_xml = ET.tostring(tree.getroot(), encoding='utf-8', method='xml')
            parsed_xml = parseString(raw_xml)
            pretty_xml = parsed_xml.toprettyxml(indent="  ")

            with open(self.output_path, 'w', encoding='utf-8') as file:
                file.write(pretty_xml)

            logger.info(f"Sitemap has been successfully saved to {self.output_path}")
        except IOError as e:
            logger.error(f"Error writing sitemap to file: {e}")
