import sqlite3
import os


class DatabaseManager:
    path = os.getcwd() + '/db'
    db_path = path + '/database.db'

    def __init__(self):
        if not os.path.exists(self.path):
            os.makedirs(self.path)

        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.conn:
            self.conn.close()

    def create_tables(self):
        try:
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    status INTEGER NOT NULL CHECK (status IN (0, 1))
                )
            ''')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_url ON pages (url)')
            self.conn.commit()
        except sqlite3.Error as e:
            print("Error creating tables:", e)

    def is_link(self, url):
        query = "SELECT * FROM pages WHERE url = ?"
        self.cursor.execute(query, (url,))
        existing_record = self.cursor.fetchone()
        return existing_record is not None

    def get_next_page(self):
        query = "SELECT * FROM pages WHERE status = 0 ORDER BY RANDOM() LIMIT 1"
        self.cursor.execute(query)
        existing_record = self.cursor.fetchone()
        return existing_record[1] if existing_record else None

    def save_page_status(self, url, status):
        try:
            query = "SELECT * FROM pages WHERE url = ?"
            self.cursor.execute(query, (url,))
            existing_record = self.cursor.fetchone()

            if existing_record:
                query = "UPDATE pages SET status = ? WHERE url = ?"
                self.cursor.execute(query, (status, url))
            else:
                query = "INSERT INTO pages (url, status) VALUES (?, ?)"
                self.cursor.execute(query, (url, status))

            self.conn.commit()
        except sqlite3.Error as e:
            print("Save page status error:", e)

    def save_links(self, links):
        unique_links = self.get_unique_links(links)
        self.cursor.executemany('INSERT OR IGNORE INTO pages (url, status) VALUES (?, 0)', [(link,) for link in unique_links])
        self.conn.commit()

    def get_unique_links(self, links):
        existing_links = self.get_existing_links(links)
        return [link for link in links if link not in existing_links]

    def get_existing_links(self, links):
        placeholders = ','.join('?' for _ in links)
        query = f'SELECT url FROM pages WHERE url IN ({placeholders})'
        self.cursor.execute(query, links)
        return set(row[0] for row in self.cursor.fetchall())

    def drop_tables(self):
        tables = ["pages"]
        for table in tables:
            self.cursor.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.commit()
