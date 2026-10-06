"""CLI Utility script to list user accounts or reset passwords in the RadioNet database."""
import sys
import os
import argparse

# Add repo root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.orm.attributes import flag_modified
from app.database import SessionLocal, engine, Base
from app.models import UserDB
from app.security import hash_password

def list_users():
    """List all registered users in the database."""
    db = SessionLocal()
    try:
        # Ensure tables exist
        Base.metadata.create_all(bind=engine)
        users = db.query(UserDB).all()
        if not users:
            print("No users found in the database.")
            return
        print("\nRegistered Users:")
        print("-" * 60)
        print(f"{'ID':<6} | {'Role':<15} | {'Email':<30} | {'Name'}")
        print("-" * 60)
        for u in users:
            print(f"{u.id:<6} | {u.role:<15} | {u.email:<30} | {u.name}")
        print("-" * 60)
    finally:
        db.close()

def reset_password(email: str, new_password: str):
    """Reset the password for a user with the given email."""
    db = SessionLocal()
    try:
        Base.metadata.create_all(bind=engine)
        user = db.query(UserDB).filter(UserDB.email.ilike(email)).first()
        if not user:
            print(f"Error: User with email '{email}' not found.")
            return False

        meta = dict(user.metadata_ or {})
        meta["password"] = hash_password(new_password)
        user.metadata_ = meta
        flag_modified(user, "metadata_")
        db.commit()
        print(f"SUCCESS: Password for user '{email}' has been successfully reset!")
        return True
    finally:
        db.close()

def create_user(email: str, name: str, role: str, password: str):
    """Create a new user with the specified email, name, role, and password."""
    db = SessionLocal()
    try:
        Base.metadata.create_all(bind=engine)
        existing = db.query(UserDB).filter(UserDB.email.ilike(email)).first()
        if existing:
            print(f"User with email '{email}' already exists (ID: {existing.id}, Role: {existing.role}). Updating password instead.")
            return reset_password(email, password)

        hashed = hash_password(password)
        new_u = UserDB(
            email=email,
            name=name,
            role=role.upper(),
            metadata_={"password": hashed}
        )
        db.add(new_u)
        db.commit()
        print(f"SUCCESS: Created user '{email}' ({role.upper()}) successfully!")
        return True
    finally:
        db.close()

def main():
    parser = argparse.ArgumentParser(description="RadioNet User Management & Password Reset Utility")
    subparsers = parser.add_subparsers(dest="command")

    # List command
    subparsers.add_parser("list", help="List all users")

    # Reset command
    reset_parser = subparsers.add_parser("reset", help="Reset password for an existing user")
    reset_parser.add_argument("email", help="Email of the user")
    reset_parser.add_argument("password", help="New password to set")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new user")
    create_parser.add_argument("email", help="Email of the new user")
    create_parser.add_argument("password", help="Password for the new user")
    create_parser.add_argument("--name", default="Admin User", help="Full name of the user")
    create_parser.add_argument("--role", default="SUPER_ADMIN", choices=["SUPER_ADMIN", "DOCTOR", "CENTER", "MANAGER"], help="Role of the user")

    args = parser.parse_args()

    if args.command == "list":
        list_users()
    elif args.command == "reset":
        reset_password(args.email, args.password)
    elif args.command == "create":
        create_user(args.email, args.name, args.role, args.password)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
