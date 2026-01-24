from src.bot.config import app
from src.bot.database import init_db

# Import all modules to register handlers
import src.bot.login
import src.bot.handlers
import src.bot.admin
import src.bot.info

if __name__ == "__main__":
    print("Initializing database...")
    init_db()
    print("Starting bot...")
    app.run()
