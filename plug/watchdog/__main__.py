from plug.config import load_dotenv

from .cli import app

load_dotenv()

if __name__ == "__main__":
    app()
