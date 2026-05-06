"""  
MongoDB On-Prem to Atlas User Migration Script (All Databases)  
  
This script:  
1. Connects to an on-prem MongoDB instance using pymongo  
2. Reads ALL database users from ALL databases and their roles  
3. Creates corresponding users in Atlas via the Atlas Admin API  
  
Prerequisites:  
    pip install pymongo requests  
  
Configuration:  
    Set the variables below or use environment variables.  
"""  
  
import os  
import sys  
import json  
import logging  
import requests  
from requests.auth import HTTPDigestAuth  
from pymongo import MongoClient  
  
# ---------------------------------------------------------------------------  
# Configuration  
# ---------------------------------------------------------------------------  
  
# On-prem MongoDB connection string  
SOURCE_MONGO_URI = os.environ.get("SOURCE_MONGO_URI", "mongodb://admin:password@localhost:27017/?authSource=admin")  
  
# Atlas Admin API credentials (public/private key pair)  
ATLAS_PUBLIC_KEY = os.environ.get("ATLAS_PUBLIC_KEY", "your-atlas-public-key")  
ATLAS_PRIVATE_KEY = os.environ.get("ATLAS_PRIVATE_KEY", "your-atlas-private-key")  
  
# Atlas project/group ID  
ATLAS_PROJECT_ID = os.environ.get("ATLAS_PROJECT_ID", "your-atlas-project-id")  
  
# Atlas Admin API base URL  
ATLAS_API_BASE_URL = "https://cloud.mongodb.com/api/atlas/v2"  
  
# Default password for migrated users (users should change this immediately)  
# Atlas requires a password for SCRAM users  
DEFAULT_PASSWORD = os.environ.get("DEFAULT_PASSWORD", "ChangeMe123!@#Temporary")  
  
# Dry run mode - set to False to actually create users  
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"  
  
# ---------------------------------------------------------------------------  
# Logging  
# ---------------------------------------------------------------------------  
  
logging.basicConfig(  
    level=logging.INFO,  
    format="%(asctime)s [%(levelname)s] %(message)s",  
    handlers=[  
        logging.StreamHandler(sys.stdout),  
        logging.FileHandler("migration.log"),  
    ],  
)  
logger = logging.getLogger(__name__)  
  
# ---------------------------------------------------------------------------  
# Roles that are NOT available in Atlas and should be dropped  
# ---------------------------------------------------------------------------  
  
# These roles either don't exist in Atlas or are managed internally by Atlas  
ROLES_TO_DROP = {  
    "__system",  
    "__queryableBackup",  
    "backup",  
    "restore",  
    "clusterAdmin",  
    "clusterManager",  
    "hostManager",  
    "bypassWriteBlockingMode",
    "userAdminAnyDatabase",  
    "userAdmin",  
}  
  
# Databases that Atlas doesn't allow user-defined roles/access on  
DATABASES_TO_SKIP = {"local", "config"}  
  
# Atlas built-in database roles that are valid  
ATLAS_VALID_ROLES = {  
    "atlasAdmin",  
    "readWriteAnyDatabase",  
    "readAnyDatabase",  
    "read",  
    "readWrite",  
    "dbAdmin",  
    "dbAdminAnyDatabase",  
    "clusterMonitor",  
    "enableSharding",  
}  
  
# Users to skip (system users that shouldn't be migrated)  
SYSTEM_USERS_TO_SKIP = {  
    "__system",  
    "mms-automation",  
    "mms-monitoring-agent",  
    "mms-backup-agent",  
}  
  
# ---------------------------------------------------------------------------  
# Helper Functions  
# ---------------------------------------------------------------------------  
  
  
def should_drop_role(role_name: str, role_db: str) -> bool:  
    """  
    Determine if a role should be dropped during migration.  
  
    Args:  
        role_name: The name of the role  
        role_db: The database the role is scoped to  
  
    Returns:  
        True if the role should be dropped, False otherwise  
    """  
    # Drop roles that are explicitly not available in Atlas  
    if role_name in ROLES_TO_DROP:  
        return True  
  
    # Drop any role scoped to databases Atlas doesn't expose  
    if role_db in DATABASES_TO_SKIP:  
        return True  
  
    return False  
  
  
def transform_roles_for_atlas(roles: list) -> list:  
    """  
    Transform on-prem roles to Atlas-compatible roles.  
    Drops roles not available in Atlas.  
  
    Args:  
        roles: List of role documents from on-prem MongoDB  
  
    Returns:  
        List of role documents compatible with Atlas Admin API  
    """  
    atlas_roles = []  
    dropped_roles = []  
  
    for role in roles:  
        role_name = role.get("role", "")  
        role_db = role.get("db", "admin")  
  
        if should_drop_role(role_name, role_db):  
            dropped_roles.append({"role": role_name, "db": role_db})  
            continue  
  
        atlas_roles.append({  
            "roleName": role_name,  
            "databaseName": role_db,  
        })  
  
    if dropped_roles:  
        logger.warning(f"  Dropped incompatible roles: {dropped_roles}")  
  
    return atlas_roles  
  
  
def get_all_users_from_all_databases(client: MongoClient) -> list:  
    """  
    Retrieve users from ALL databases on the MongoDB instance.  
  
    MongoDB can store users in individual databases' system.users collections,  
    though the canonical source is admin.system.users. This function checks  
    all databases to ensure complete coverage.  
  
    Args:  
        client: Connected MongoClient instance  
  
    Returns:  
        Deduplicated list of user documents  
    """  
    all_users = {}  
  
    # Method 1: Use the admin database's usersInfo command with showAllUsers  
    # This should return users from all databases  
    logger.info("Fetching users via admin.usersInfo (all users)...")  
    try:  
        result = client.admin.command({"usersInfo": 1, "showCredentials": False})  
        users = result.get("users", [])  
        logger.info(f"  Found {len(users)} users via admin.usersInfo")  
        for user in users:  
            key = f"{user.get('db', 'admin')}.{user.get('user', '')}"  
            all_users[key] = user  
    except Exception as e:  
        logger.warning(f"  Failed to get users via admin.usersInfo: {e}")  
  
    # Method 2: Query admin.system.users collection directly  
    # This is the authoritative collection for all users in MongoDB 3.0+  
    logger.info("Fetching users from admin.system.users collection...")  
    try:  
        system_users = list(client.admin.system.users.find({}))  
        logger.info(f"  Found {len(system_users)} users in admin.system.users")  
        for user in system_users:  
            username = user.get("user", "")  
            auth_db = user.get("db", "admin")  
            key = f"{auth_db}.{username}"  
            if key not in all_users:  
                all_users[key] = user  
    except Exception as e:  
        logger.warning(f"  Failed to query admin.system.users: {e}")  
  
    # Method 3: Iterate through all databases and run usersInfo on each  
    # This catches edge cases where users might be defined in specific databases  
    logger.info("Fetching users from individual databases...")  
    try:  
        database_names = client.list_database_names()  
        logger.info(f"  Found {len(database_names)} databases: {database_names}")  
  
        for db_name in database_names:  
            if db_name in DATABASES_TO_SKIP:  
                logger.info(f"  Skipping database '{db_name}' (not supported in Atlas)")  
                continue  
  
            try:  
                db = client[db_name]  
                result = db.command({"usersInfo": 1, "showCredentials": False})  
                db_users = result.get("users", [])  
  
                if db_users:  
                    logger.info(f"  Found {len(db_users)} users in database '{db_name}'")  
                    for user in db_users:  
                        username = user.get("user", "")  
                        auth_db = user.get("db", db_name)  
                        key = f"{auth_db}.{username}"  
                        if key not in all_users:  
                            all_users[key] = user  
            except Exception as e:  
                logger.debug(f"  Could not query users in database '{db_name}': {e}")  
  
    except Exception as e:  
        logger.warning(f"  Failed to list databases: {e}")  
  
    # Method 4: Check $external database for LDAP/X.509 users  
    logger.info("Checking for $external (LDAP/X.509) users...")  
    try:  
        result = client.admin.command({  
            "usersInfo": 1,  
            "filter": {"db": "$external"},  
            "showCredentials": False,  
        })  
        external_users = result.get("users", [])  
        if external_users:  
            logger.info(f"  Found {len(external_users)} $external users")  
            for user in external_users:  
                key = f"$external.{user.get('user', '')}"  
                if key not in all_users:  
                    all_users[key] = user  
    except Exception as e:  
        logger.debug(f"  Could not query $external users: {e}")  
  
    deduplicated_users = list(all_users.values())  
    logger.info(f"\nTotal unique users found across all databases: {len(deduplicated_users)}")  
  
    return deduplicated_users  
  
  
def get_source_users(mongo_uri: str) -> list:  
    """  
    Connect to the source MongoDB instance and retrieve all users  
    from all databases.  
  
    Args:  
        mongo_uri: MongoDB connection string for the source instance  
  
    Returns:  
        List of user documents  
    """  
    logger.info("Connecting to source MongoDB instance...")  
  
    try:  
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)  
        # Force connection to verify connectivity  
        client.admin.command("ping")  
        logger.info("Successfully connected to source MongoDB.")  
    except Exception as e:  
        logger.error(f"Failed to connect to source MongoDB: {e}")  
        sys.exit(1)  
  
    try:  
        # Get server info for context  
        server_info = client.server_info()  
        logger.info(f"Source MongoDB version: {server_info.get('version', 'unknown')}")  
  
        # Get all users from all databases  
        users = get_all_users_from_all_databases(client)  
        return users  
    except Exception as e:  
        logger.error(f"Failed to retrieve users: {e}")  
        sys.exit(1)  
    finally:  
        client.close()  
  
  
def create_atlas_user(username: str, database: str, roles: list, password: str) -> dict:  
    """  
    Create a database user in Atlas via the Admin API.  
  
    Atlas Admin API endpoint:  
    POST /api/atlas/v2/groups/{groupId}/databaseUsers  
  
    Args:  
        username: The username for the new user  
        database: The authentication database (usually "admin" or "$external")  
        roles: List of Atlas-formatted role documents  
        password: The password for the user  
  
    Returns:  
        API response as a dictionary  
    """  
    url = f"{ATLAS_API_BASE_URL}/groups/{ATLAS_PROJECT_ID}/databaseUsers"  
  
    payload = {  
        "databaseName": "admin",  # auth database  
        "username": username,  
        "roles": roles,  
        "groupId": ATLAS_PROJECT_ID,  
    }  
  
    # Only include password for SCRAM auth users (databaseName = "admin" or other dbs)  
    # For X.509 or LDAP users (databaseName = "$external"), no password is needed  
    if database != "$external":  
        payload["password"] = password  
  
    # For $external users, set the appropriate auth type  
    if database == "$external":  
        # Determine if it's X.509 or LDAP based on username format  
        if "CN=" in username or "cn=" in username.lower():  
            payload["x509Type"] = "CUSTOMER"  
        else:  
            payload["ldapAuthType"] = "USER"  
  
    headers = {  
        "Content-Type": "application/json",  
        "Accept": "application/vnd.atlas.2023-02-01+json",  
    }  
  
    if DRY_RUN:  
        # Mask password in log output  
        log_payload = {**payload}  
        if "password" in log_payload:  
            log_payload["password"] = "********"  
        logger.info(f"  [DRY RUN] Would create user with payload: {json.dumps(log_payload, indent=2)}")  
        return {"dryRun": True, "status": "skipped"}  
  
    try:  
        response = requests.post(  
            url,  
            auth=HTTPDigestAuth(ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY),  
            headers=headers,  
            json=payload,  
        )  
  
        if response.status_code == 201:  
            logger.info(f"  Successfully created user '{username}' (authDB: {database}) in Atlas.")  
            return response.json()  
        elif response.status_code == 409:  
            logger.warning(f"  User '{username}' (authDB: {database}) already exists in Atlas. Skipping.")  
            return {"status": "already_exists", "code": 409}  
        else:  
            logger.error(  
                f"  Failed to create user '{username}' (authDB: {database}). "  
                f"Status: {response.status_code}, Response: {response.text}"  
            )  
            return {"status": "error", "code": response.status_code, "detail": response.text}  
  
    except requests.exceptions.RequestException as e:  
        logger.error(f"  Request failed for user '{username}': {e}")  
        return {"status": "error", "detail": str(e)}  
  
  
def migrate_users():  
    """  
    Main migration function that orchestrates reading users from source  
    and creating them in Atlas.  
    """  
    logger.info("=" * 60)  
    logger.info("MongoDB User Migration: On-Prem -> Atlas (All Databases)")  
    logger.info("=" * 60)  
  
    if DRY_RUN:  
        logger.info("*** DRY RUN MODE - No users will be created ***")  
        logger.info("")  
  
    # Step 1: Read users from ALL databases on source  
    users = get_source_users(SOURCE_MONGO_URI)  
  
    if not users:  
        logger.info("No users found to migrate.")  
        return  
  
    # Step 2: Process and migrate each user  
    results = {  
        "success": [],  
        "skipped": [],  
        "failed": [],  
        "no_roles": [],  
    }  
  
    # Group users by auth database for clear reporting  
    users_by_db = {}  
    for user in users:  
        auth_db = user.get("db", "admin")  
        if auth_db not in users_by_db:  
            users_by_db[auth_db] = []  
        users_by_db[auth_db].append(user)  
  
    logger.info("\nUsers by authentication database:")  
    for db, db_users in users_by_db.items():  
        logger.info(f"  {db}: {len(db_users)} users")  
  
    logger.info("\n" + "-" * 60)  
    logger.info("Processing users...")  
    logger.info("-" * 60)  
  
    for user in users:  
        username = user.get("user", "")  
        auth_db = user.get("db", "admin")  
        roles = user.get("roles", [])  
        mechanisms = user.get("mechanisms", [])  
  
        logger.info(f"\nProcessing user: '{username}' (authDB: '{auth_db}')")  
        logger.info(f"  Original roles: {roles}")  
        if mechanisms:  
            logger.info(f"  Auth mechanisms: {mechanisms}")  
  
        # Skip system/internal users  
        if username in SYSTEM_USERS_TO_SKIP:  
            logger.info(f"  Skipping system user '{username}'.")  
            results["skipped"].append(f"{auth_db}.{username}")  
            continue  
  
        # Skip users with no username  
        if not username:  
            logger.warning("  Skipping user with empty username.")  
            results["skipped"].append(f"{auth_db}.(empty)")  
            continue  
  
        # Skip users whose auth database is local or config  
        if auth_db in DATABASES_TO_SKIP:  
            logger.info(f"  Skipping user '{username}' - auth database '{auth_db}' not supported in Atlas.")  
            results["skipped"].append(f"{auth_db}.{username}")  
            continue  
  
        # Transform roles for Atlas compatibility  
        atlas_roles = transform_roles_for_atlas(roles)  
  
        if not atlas_roles:  
            logger.warning(  
                f"  User '{username}' (authDB: '{auth_db}') has no valid Atlas roles "  
                f"after filtering. Skipping user."  
            )  
            results["no_roles"].append(f"{auth_db}.{username}")  
            continue  
  
        logger.info(f"  Atlas roles: {atlas_roles}")  
  
        # Create user in Atlas  
        response = create_atlas_user(  
            username=username,  
            database=auth_db,  
            roles=atlas_roles,  
            password=DEFAULT_PASSWORD,  
        )  
  
        if response.get("status") == "error":  
            results["failed"].append(f"{auth_db}.{username}")  
        elif response.get("status") == "already_exists":  
            results["skipped"].append(f"{auth_db}.{username}")  
        else:  
            results["success"].append(f"{auth_db}.{username}")  
  
    # Step 3: Print summary  
    logger.info("\n" + "=" * 60)  
    logger.info("Migration Summary")  
    logger.info("=" * 60)  
    logger.info(f"  Total users found:     {len(users)}")  
    logger.info(f"  Successfully created:  {len(results['success'])}")  
    logger.info(f"  Skipped:               {len(results['skipped'])}")  
    logger.info(f"  No valid roles:        {len(results['no_roles'])}")  
    logger.info(f"  Failed:                {len(results['failed'])}")  
  
    if results["success"]:  
        logger.info(f"\n  Created users: {results['success']}")  
  
    if results["failed"]:  
        logger.error(f"\n  Failed users: {results['failed']}")  
  
    if results["no_roles"]:  
        logger.warning(f"\n  Users with no valid Atlas roles: {results['no_roles']}")  
  
    if results["skipped"]:  
        logger.info(f"\n  Skipped users: {results['skipped']}")  
  
    if not DRY_RUN and results["success"]:  
        logger.warning(  
            "\n⚠️  IMPORTANT: All migrated SCRAM users have been created with a "  
            "temporary password. Please ensure users reset their passwords "  
            "immediately!"  
        )  
  
  
# ---------------------------------------------------------------------------  
# Entry Point  
# ---------------------------------------------------------------------------  
  
if __name__ == "__main__":  
    migrate_users()  
