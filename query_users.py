import sqlite3
import os

# Full path to your StudyBudy database
db_path = r'c:/Users/AAMIR SHAMSI/Agentic_Ai/TutorAi/instance/database.db'

if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password FROM user")
    rows = cursor.fetchall()
    
    if rows:
        print("Users in database:")
        for row in rows:
            print(f"ID: {row[0]}, Username: {row[1]}, Password Hash: {row[2][:50]}...")
    else:
        print("No users found in the database.")
    
    conn.close()
