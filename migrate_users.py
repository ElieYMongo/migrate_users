"""  
MongoDB On-Prem to Atlas User Migration Script  
  
This script:  
1. Connects to an on-prem MongoDB instance using pymongo  
2. Reads all database users and their roles  
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
    "userAdmin",  # only dropped when on 'local' db; handled in logic below  
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
    "userAdminAnyDatabase",  
    "userAdmin",  
    "clusterMonitor",  
    "enableSharding",  
    "dbAdminAnyDatabase",  
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
        # Some roles like userAdmin are fine on non-local databases  
        if role_name == "userAdmin" and role_db not in DATABASES_TO_SKIP:  
            return False  
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
  
  
def get_source_users(mongo_uri: str) -> list:  
    """  
    Connect to the source MongoDB instance and retrieve all users.  
      
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
  
    # Get all users from the admin database  
    # The usersInfo command returns all users across all databases  
    try:  
        result = client.admin.command("usersInfo", 1)  # 1 = show all users  
        users = result.get("users", [])  
        logger.info(f"Found {len(users)} users in source database.")  
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
        "databaseName": database,  # auth database  
        "username": username,  
        "roles": roles,  
    }  
  
    # Only include password for SCRAM auth users (databaseName = "admin")  
    # For X.509 or LDAP users (databaseName = "$external"), no password is needed  
    if database == "admin":  
        payload["password"] = password  
  
    headers = {  
        "Content-Type": "application/json",  
        "Accept": "application/vnd.atlas.2023-02-01+json",  
    }  
  
    if DRY_RUN:  
        logger.info(f"  [DRY RUN] Would create user with payload: {json.dumps(payload, indent=2)}")  
        return {"dryRun": True, "status": "skipped"}  
  
    try:  
        response = requests.post(  
            url,  
            auth=HTTPDigestAuth(ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY),  
            headers=headers,  
            json=payload,  
        )  
  
        if response.status_code == 201:  
            logger.info(f"  Successfully created user '{username}' in Atlas.")  
            return response.json()  
        elif response.status_code == 409:  
            logger.warning(f"  User '{username}' already exists in Atlas. Skipping.")  
            return {"status": "already_exists", "code": 409}  
        else:  
            logger.error(  
                f"  Failed to create user '{username}'. "  
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
    logger.info("MongoDB User Migration: On-Prem -> Atlas")  
    logger.info("=" * 60)  
  
    if DRY_RUN:  
        logger.info("*** DRY RUN MODE - No users will be created ***")  
        logger.info("")  
  
    # Step 1: Read users from source  
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
  
    # Users to skip (system users that shouldn't be migrated)  
    system_users_to_skip = {"__system", "mms-automation", "mms-monitoring-agent", "mms-backup-agent"}  
  
    for user in users:  
        username = user.get("user", "")  
        auth_db = user.get("db", "admin")  
        roles = user.get("roles", [])  
        mechanisms = user.get("mechanisms", [])  
  
        logger.info(f"\nProcessing user: '{username}' (authDB: {auth_db})")  
        logger.info(f"  Original roles: {roles}")  
        logger.info(f"  Auth mechanisms: {mechanisms}")  
  
        # Skip system/internal users  
        if username in system_users_to_skip:  
            logger.info(f"  Skipping system user '{username}'.")  
            results["skipped"].append(username)  
            continue  
  
        # Skip users with no username  
        if not username:  
            logger.warning("  Skipping user with empty username.")  
            results["skipped"].append("(empty)")  
            continue  
  
        # Transform roles for Atlas compatibility  
        atlas_roles = transform_roles_for_atlas(roles)  
  
        if not atlas_roles:  
            logger.warning(  
                f"  User '{username}' has no valid Atlas roles after filtering. "  
                f"Skipping user."  
            )  
            results["no_roles"].append(username)  
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
            results["failed"].append(username)  
        elif response.get("status") == "already_exists":  
            results["skipped"].append(username)  
        else:  
            results["success"].append(username)  
  
    # Step 3: Print summary  
    logger.info("\n" + "=" * 60)  
    logger.info("Migration Summary")  
    logger.info("=" * 60)  
    logger.info(f"  Total users processed: {len(users)}")  
    logger.info(f"  Successfully created:  {len(results['success'])}")  
    logger.info(f"  Skipped:               {len(results['skipped'])}")  
    logger.info(f"  No valid roles:        {len(results['no_roles'])}")  
    logger.info(f"  Failed:                {len(results['failed'])}")  
  
    if results["failed"]:  
        logger.error(f"  Failed users: {results['failed']}")  
  
    if results["no_roles"]:  
        logger.warning(f"  Users with no valid Atlas roles: {results['no_roles']}")  
  
    if not DRY_RUN and results["success"]:  
        logger.warning(  
            "\n⚠️  IMPORTANT: All migrated users have been created with a "  
            "temporary password. Please ensure users reset their passwords "  
            "immediately!"  
        )  
  
  
# ---------------------------------------------------------------------------  
# Entry Point  
# ---------------------------------------------------------------------------  
  
if __name__ == "__main__":  
    migrate_users()  
