# CLI Utility for Web Scraping

This program is a command-line tool for web scraping that allows you to parse URLs, update existing data, and export results in the `sitemap.xml` format.

## Requirements

- Python 3.7 or higher
- Installed `selenium` module
- Installed web browser driver (e.g., ChromeDriver for Google Chrome)
- Installed modules: `argparse`, `xml.etree.ElementTree`, `logging`

## Installation

1. **Clone the repository**

   Clone the repository to your local machine:

    ```
       git clone https://github.com/your-username/your-repo.git
       cd your-repo
    ```

2. **Create a virtual environment (recommended)**
    Create and activate a virtual environment:
    ```
    python -m venv venv
    source venv/bin/activate  # For Linux/MacOS
    .\venv\Scripts\activate   # For Windows
   ```
    
3. **Install dependencies**
    ```pip install -r requirements.txt```

4. **Set up the driver**
    Download the driver for your browser (e.g., ChromeDriver) and add it to your system's PATH environment variable.

## Usage
### Command Structure
The program supports the following commands:

```new```: Delete old data from the database and perform a clean parse.

```update```: Continue an existing parse.

```export```: Export data to sitemap.xml.

### Running the Program
To run the program, use the following command format:

```
python run.py <command>
```

### Examples
#### Execute new parsing

```python run.py new```

#### Update existing parsing
```python run.py update```

#### Export data

```python run.py export```

## Configuration
Before the first run of the program, you need to set up the configuration file config.json.
Here’s an example:
```
{
    "url": "https://example.com",
    "delay": 2
}
```

```url```: The URL to start parsing from.

```delay```: The delay (in seconds) between requests to avoid getting blocked.
