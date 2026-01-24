from bot.config import app
from bot.database import init_db

# Import all modules to register handlers
import bot.login
import bot.handlers
import bot.admin
import bot.info

if __name__ == "__main__":
    print("Initializing database...")
    init_db()
    print("Starting bot...")
    app.run()
