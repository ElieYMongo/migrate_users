"""  
Atlas Database Users Rollback Script  
  
This script:  
1. Lists all database users in an Atlas project via the Admin API  
2. Deletes each user, effectively rolling back a migration  
  
Prerequisites:  
    pip install requests  
  
Configuration:  
    Set the variables below or use environment variables.  
  
WARNING: This script will DELETE ALL database users in the specified project.  
         Use with extreme caution!  
"""  
  
import os  
import sys  
import json  
import logging  
import requests  
from requests.auth import HTTPDigestAuth  
from urllib.parse import quote  
  
# ---------------------------------------------------------------------------  
# Configuration  
# ---------------------------------------------------------------------------  
  
# Atlas Admin API credentials (public/private key pair)  
ATLAS_PUBLIC_KEY = os.environ.get("ATLAS_PUBLIC_KEY", "your-atlas-public-key")  
ATLAS_PRIVATE_KEY = os.environ.get("ATLAS_PRIVATE_KEY", "your-atlas-private-key")  
  
# Atlas project/group ID  
ATLAS_PROJECT_ID = os.environ.get("ATLAS_PROJECT_ID", "your-atlas-project-id")  
  
# Atlas Admin API base URL  
ATLAS_API_BASE_URL = "https://cloud.mongodb.com/api/atlas/v2"  
  
# Dry run mode - set to False to actually delete users  
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"  
  
# Optional: Only delete users that were part of the migration  
# If set, only users listed in this file will be deleted  
# Leave empty or unset to delete ALL users in the project  
USERS_FILTER_FILE = os.environ.get("USERS_FILTER_FILE", "")  
  
# Users to NEVER delete (safety net)  
PROTECTED_USERS = set(  
    os.environ.get("PROTECTED_USERS", "").split(",")  
) if os.environ.get("PROTECTED_USERS") else set()  
  
# ---------------------------------------------------------------------------  
# Logging  
# ---------------------------------------------------------------------------  
  
logging.basicConfig(  
    level=logging.INFO,  
    format="%(asctime)s [%(levelname)s] %(message)s",  
    handlers=[  
        logging.StreamHandler(sys.stdout),  
        logging.FileHandler("rollback.log"),  
    ],  
)  
logger = logging.getLogger(__name__)  
  
# ---------------------------------------------------------------------------  
# Helper Functions  
# ---------------------------------------------------------------------------  
  
  
def get_api_headers() -> dict:  
    """Return standard headers for Atlas Admin API requests."""  
    return {  
        "Content-Type": "application/json",  
        "Accept": "application/vnd.atlas.2023-02-01+json",  
    }  
  
  
def load_users_filter(filepath: str) -> set:  
    """  
    Load a list of usernames to filter deletions.  
    Only users in this list will be deleted.  
  
    Expected format (same as passwords.txt):  
        username,password  
        user1,P@ssw0rd123  
        user2,P@ssw0rd124  
  
    Or simple list:  
        user1  
        user2  
  
    Args:  
        filepath: Path to the filter file  
  
    Returns:  
        Set of usernames to delete  
    """  
    if not filepath or not os.path.exists(filepath):  
        return set()  
  
    usernames = set()  
    try:  
        with open(filepath, "r", encoding="utf-8") as f:  
            for line_num, line in enumerate(f, 1):  
                line = line.strip()  
                if not line:  
                    continue  
  
                # Skip header line  
                if line_num == 1 and ("username" in line.lower()):  
                    continue  
  
                # Handle CSV format (username,password)  
                if "," in line:  
                    username = line.split(",")[0].strip()  
                else:  
                    username = line.strip()  
  
                if username:  
                    usernames.add(username)  
  
        logger.info(f"Loaded {len(usernames)} usernames from filter file '{filepath}'")  
        return usernames  
  
    except IOError as e:  
        logger.error(f"Error reading filter file: {e}")  
        return set()  
  
  
def list_atlas_users() -> list:  
    """  
    List all database users in the Atlas project.  
  
    Atlas Admin API endpoint:  
    GET /api/atlas/v2/groups/{groupId}/databaseUsers  
  
    Returns:  
        List of user documents from Atlas  
    """  
    url = f"{ATLAS_API_BASE_URL}/groups/{ATLAS_PROJECT_ID}/databaseUsers"  
    headers = get_api_headers()  
  
    all_users = []  
    page_num = 1  
    items_per_page = 100  
  
    logger.info("Fetching database users from Atlas...")  
  
    while True:  
        params = {  
            "pageNum": page_num,  
            "itemsPerPage": items_per_page,  
        }  
  
        try:  
            response = requests.get(  
                url,  
                auth=HTTPDigestAuth(ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY),  
                headers=headers,  
                params=params,  
            )  
  
            if response.status_code != 200:  
                logger.error(  
                    f"Failed to list users. Status: {response.status_code}, "  
                    f"Response: {response.text}"  
                )  
                sys.exit(1)  
  
            data = response.json()  
            results = data.get("results", [])  
            total_count = data.get("totalCount", 0)  
  
            all_users.extend(results)  
  
            logger.info(  
                f"  Page {page_num}: Retrieved {len(results)} users "  
                f"({len(all_users)}/{total_count} total)"  
            )  
  
            # Check if there are more pages  
            if len(all_users) >= total_count:  
                break  
  
            page_num += 1  
  
        except requests.exceptions.RequestException as e:  
            logger.error(f"Request failed while listing users: {e}")  
            sys.exit(1)  
  
    logger.info(f"Total database users found in Atlas: {len(all_users)}")  
    return all_users  
  
  
def delete_atlas_user(username: str, database: str) -> dict:  
    """  
    Delete a database user from Atlas via the Admin API.  
  
    Atlas Admin API endpoint:  
    DELETE /api/atlas/v2/groups/{groupId}/databaseUsers/{databaseName}/{username}  
  
    Args:  
        username: The username to delete  
        database: The authentication database (e.g., "admin", "$external")  
  
    Returns:  
        Result dictionary with status information  
    """  
    # URL-encode the username and database name to handle special characters  
    encoded_username = quote(username, safe="")  
    encoded_database = quote(database, safe="")  
  
    url = (  
        f"{ATLAS_API_BASE_URL}/groups/{ATLAS_PROJECT_ID}"  
        f"/databaseUsers/{encoded_database}/{encoded_username}"  
    )  
    headers = get_api_headers()  
  
    if DRY_RUN:  
        logger.info(f"  [DRY RUN] Would delete user '{username}' (authDB: '{database}')")  
        return {"status": "dry_run", "username": username, "database": database}  
  
    try:  
        response = requests.delete(  
            url,  
            auth=HTTPDigestAuth(ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY),  
            headers=headers,  
        )  
  
        if response.status_code == 204:  
            logger.info(f"  Successfully deleted user '{username}' (authDB: '{database}')")  
            return {"status": "success", "username": username, "database": database}  
        elif response.status_code == 404:  
            logger.warning(f"  User '{username}' (authDB: '{database}') not found. Already deleted?")  
            return {"status": "not_found", "username": username, "database": database}  
        else:  
            logger.error(  
                f"  Failed to delete user '{username}' (authDB: '{database}'). "  
                f"Status: {response.status_code}, Response: {response.text}"  
            )  
            return {  
                "status": "error",  
                "username": username,  
                "database": database,  
                "code": response.status_code,  
                "detail": response.text,  
            }  
  
    except requests.exceptions.RequestException as e:  
        logger.error(f"  Request failed for user '{username}': {e}")  
        return {"status": "error", "username": username, "database": database, "detail": str(e)}  
  
  
def confirm_deletion(users_to_delete: list) -> bool:  
    """  
    Prompt the user for confirmation before deleting.  
  
    Args:  
        users_to_delete: List of user documents that will be deleted  
  
    Returns:  
        True if user confirms, False otherwise  
    """  
    print("\n" + "!" * 60)  
    print("WARNING: You are about to DELETE the following users:")  
    print("!" * 60)  
  
    for user in users_to_delete:  
        username = user.get("username", "")  
        database = user.get("databaseName", "admin")  
        roles = user.get("roles", [])  
        print(f"  - {username} (authDB: {database}, roles: {len(roles)})")  
  
    print(f"\n  Total users to delete: {len(users_to_delete)}")  
    print(f"  Project ID: {ATLAS_PROJECT_ID}")  
    print("!" * 60)  
  
    response = input("\nType 'DELETE' to confirm, or anything else to cancel: ")  
    return response.strip() == "DELETE"  
  
  
def rollback_users():  
    """  
    Main rollback function that lists all Atlas database users and deletes them.  
    """  
    logger.info("=" * 60)  
    logger.info("Atlas Database Users Rollback")  
    logger.info("=" * 60)  
  
    if DRY_RUN:  
        logger.info("*** DRY RUN MODE - No users will be deleted ***")  
        logger.info("")  
  
    # Step 1: Load optional filter file  
    users_filter = set()  
    if USERS_FILTER_FILE:  
        users_filter = load_users_filter(USERS_FILTER_FILE)  
        if users_filter:  
            logger.info(f"Filter active: Only deleting users found in '{USERS_FILTER_FILE}'")  
        else:  
            logger.warning(f"Filter file specified but empty/not found: '{USERS_FILTER_FILE}'")  
  
    if PROTECTED_USERS:  
        logger.info(f"Protected users (will NOT be deleted): {PROTECTED_USERS}")  
  
    # Step 2: List all database users in the project  
    all_users = list_atlas_users()  
  
    if not all_users:  
        logger.info("No database users found in the Atlas project. Nothing to do.")  
        return  
  
    # Step 3: Display all users found  
    logger.info("\nUsers found in Atlas project:")  
    logger.info("-" * 60)  
    for user in all_users:  
        username = user.get("username", "")  
        database = user.get("databaseName", "admin")  
        roles = user.get("roles", [])  
        role_summary = [f"{r.get('roleName', '')}@{r.get('databaseName', '')}" for r in roles]  
        logger.info(f"  {username} (authDB: {database}) - Roles: {role_summary}")  
  
    # Step 4: Determine which users to delete  
    users_to_delete = []  
    users_to_skip = []  
  
    for user in all_users:  
        username = user.get("username", "")  
        database = user.get("databaseName", "admin")  
  
        # Check protected users  
        if username in PROTECTED_USERS:  
            logger.info(f"  PROTECTED - Skipping '{username}' (authDB: '{database}')")  
            users_to_skip.append(user)  
            continue  
  
        # Check filter (if active)  
        if users_filter and username not in users_filter:  
            logger.info(f"  FILTERED - Skipping '{username}' (not in filter file)")  
            users_to_skip.append(user)  
            continue  
  
        users_to_delete.append(user)  
  
    if not users_to_delete:  
        logger.info("\nNo users matched for deletion (all filtered or protected).")  
        return  
  
    logger.info(f"\nUsers to delete: {len(users_to_delete)}")  
    logger.info(f"Users to skip:   {len(users_to_skip)}")  
  
    # Step 5: Confirm deletion (skip in dry-run mode)  
    if not DRY_RUN:  
        if not confirm_deletion(users_to_delete):  
            logger.info("Deletion cancelled by user.")  
            return  
  
    # Step 6: Delete each user  
    logger.info("\n" + "-" * 60)  
    logger.info("Deleting users...")  
    logger.info("-" * 60)  
  
    results = {  
        "success": [],  
        "not_found": [],  
        "failed": [],  
        "dry_run": [],  
    }  
  
    for user in users_to_delete:  
        username = user.get("username", "")  
        database = user.get("databaseName", "admin")  
  
        logger.info(f"\nDeleting user: '{username}' (authDB: '{database}')")  
  
        result = delete_atlas_user(username, database)  
        status = result.get("status", "error")  
  
        if status == "success":  
            results["success"].append(f"{database}.{username}")  
        elif status == "not_found":  
            results["not_found"].append(f"{database}.{username}")  
        elif status == "dry_run":  
            results["dry_run"].append(f"{database}.{username}")  
        else:  
            results["failed"].append(f"{database}.{username}")  
  
    # Step 7: Print summary  
    logger.info("\n" + "=" * 60)  
    logger.info("Rollback Summary")  
    logger.info("=" * 60)  
    logger.info(f"  Total users in project:  {len(all_users)}")  
    logger.info(f"  Users targeted:          {len(users_to_delete)}")  
    logger.info(f"  Users skipped:           {len(users_to_skip)}")  
    logger.info(f"  Successfully deleted:    {len(results['success'])}")  
    logger.info(f"  Not found (already gone):{len(results['not_found'])}")  
    logger.info(f"  Failed:                  {len(results['failed'])}")  
  
    if DRY_RUN:  
        logger.info(f"  Dry run (would delete):  {len(results['dry_run'])}")  
  
    if results["success"]:  
        logger.info(f"\n  Deleted: {results['success']}")  
  
    if results["failed"]:  
        logger.error(f"\n  Failed to delete: {results['failed']}")  
  
    if results["not_found"]:  
        logger.warning(f"\n  Not found: {results['not_found']}")  
  
    if DRY_RUN:  
        logger.info(  
            "\n  To actually delete users, set DRY_RUN=false and re-run."  
        )  
  
  
# ---------------------------------------------------------------------------  
# Entry Point  
# ---------------------------------------------------------------------------  
  
if __name__ == "__main__":  
    rollback_users()  
