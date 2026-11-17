from garmin_client import GarminClient
import sqlite3
import logging 
import sys
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

logger.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

def ensure_gear_integrity(
    client: GarminClient,
    conn: sqlite3.Connection
) -> int:
    """
    Checks the integrity of the gear table compared to the gear activities table. Updates the gear activities table if necessary.
    """
    bad_gear_list = check_gear_integrity(conn)
    if len(bad_gear_list) > 0:
        fix_gear_integrity(client,conn)
    return(len(bad_gear_list))

def check_gear_integrity(conn: sqlite3.Connection) -> list[str]:
    """Returns a list of gear_id values where the activities database is inconsistent with the summary data for the gear itself
    Any of:
    * num_activity_entries does not match count of activities associated with this gear
    * distance does not match sum of activity distances associated with this gear
    NB: duration is not used because it is stored as integer seconds in the gear table/api, which leads to rounding errors
    (especially since the sqlite python library appears to lack a FLOOR function)
    """

    query = """SELECT gear_id, gear.brand, 
    gear.num_activities_linked   AS num,
    COUNT(activity_id)           AS num_c, 
    distance_used_meters         AS dist,
    SUM(distance_meters)         AS dist_s
    FROM gear LEFT JOIN activity_gear USING (gear_id) LEFT JOIN activity USING (activity_id)
    GROUP BY gear_id
    HAVING 
    gear.num_activities_linked <> COUNT(activity_id) OR 
    ROUND(distance_used_meters)       <> ROUND(SUM(distance_meters) );
    """
    cursor=conn.execute(query)
    rows = cursor.fetchall()
    uuids = list(map(lambda r: r['gear_id'],rows))
    if len(uuids)>0:
        logger.warning("The following gear items need updated activity lists due to mismatched number (num), or distance (dist) entries")
        logger.warning(f"|{'gear_id':>38} | {'brand':>14} | {'num':>5} | {'calculated':10} | {  ('distance'):8} | {(('calculated')):10} |")
        for r in rows:
                logger.warning(f"|{r['gear_id']:>38} | {r['brand'][:14]:>14} | {r['num']:>5} | {r['num_c']:>10} | {(r['dist']/1000):8.2f} | {((r['dist_s'] or 0)/1000):10.2f} |")
    else:
        logger.warning("Gear activity integrity ok.")
    return uuids



def fix_gear_integrity(
    client: GarminClient,
    conn: sqlite3.Connection,
) -> int:
    """
    Refreshes the activity_gear table by fetching the gear associated with every single activity (as listed in the database)
    Note that inconsistencies will remain in the database if the database has missing activities
    """
    cursor = conn.execute("SELECT activity_id FROM activity")
    rows = cursor.fetchall()
    i = 0
    batch_size=25
    while (i<len(rows)):
        eps = {}
        logger.warning(f"Refreshing gear for all {len(rows)} activities - batch {i+1}...{min(len(rows),i+batch_size)}")
        for j in range(batch_size):
             r = rows[i] 
             aid = r['activity_id']
             eps[aid]=f"https://connect.garmin.com/gc-api/gear-service/activity/v2/{aid}" 
             i += 1
             if i==len(rows):
                  break             
        if eps:
             batch_data = client._fetch_batch(eps,{})
             for aid,resp in batch_data.items():
                  if resp.get('status') == 200:
                        upsert_activity_gear(conn,aid,resp.get('data'))
        

def upsert_activity_gear(conn: sqlite3.Connection, activity_id: int, data) -> int:
    """
    Performs an insert of gear associated with each activity. 
    However, this association could be changed retrospectively via the GC website.
    Therefore, the integrity must be checked after an incremental update of the activities
    """
    exec_str = f"DELETE FROM activity_gear WHERE activity_id = {activity_id};"
    conn.execute(exec_str)
    if not data:
            conn.commit() #we've deleted some stuff so commit req'd
            return 0
    count = 0 
    gear_list = data if isinstance(data,list) else [data]
    for i, gear_item in enumerate(gear_list):   
        exec_str = """INSERT OR REPLACE INTO activity_gear
               (activity_id, gear_id)
               VALUES (?, ?)"""   
        # exec_str = "INSERT OR REPLACE INTO activity_gear (activity_id,  gear_number) VALUES (?, ?)"   
        conn.execute(exec_str,
                (activity_id,
                gear_item.get("uuid"),
                )
        )
        count += 1
    return count


def add_gear_to_activity(
        client: GarminClient,
        aid: int, 
        gid: str
    ) -> bool:
    url = f"https://connect.garmin.com/gc-api/gear-service/activity/v2/{aid}/associated-gear"
    url_check = f"https://connect.garmin.com/gc-api/activity-service/activity/{aid}"
    check_data = client.api_fetch(url_check)
    if check_data:
        resp = _api_put(client,url,gid)
        if resp:
             logger.info(f"Activity {aid} updated")
        else:
             logger.warning(f"Activity {aid} failed to update")
        return resp
    print(f"Activity {aid} may not exist - ignoring this request")
    return False
    

def _api_put(
          client: GarminClient, 
          api_path: str, 
          body: str) -> bool:
    """Fetch PUT JSON to a /gc-api endpoint via the browser context.
    Returns true if put request succeeded
    modified from the client.py download_file example
    """
    client._ensure_on_garmin()
    s = """
        var callback = arguments[arguments.length - 1];
        (async function() {
            try {
                var resp = await fetch('%s', {
                    method: 'PUT',
                    credentials: 'include',
                    headers: {
                        'connect-csrf-token': document.querySelector('meta[name="csrf-token"]').content, 
                        'Accept': 'application/json',
                        'Content-Type': "application/json",
                    },
                    body: JSON.stringify(['%s'])
                });
                return {status: resp.status}            
            }
            catch(e) {return {status: e}}
        })().then(callback).catch(function() { callback({status: 'error1'}); });
    """ % (api_path,body)
    out = client._driver.execute_async_script(s)
    if out and out.get("status") > 199 and out.get("status") < 300: # all successful response codes
        return True
    return False

def add_gear_to_activities_matching(
          client: GarminClient,
          conn: sqlite3.Connection,
          gid: str,
          query:str,
        ) -> int:
    """
    adds gear_id `gid` to all activity_ids matched in query `str`
    """
    cursor = conn.execute(query)
    rows = cursor.fetchall()
    
    aids = list(map(lambda r: r['activity_id'],rows))
    print(aids)
    #TODO - could make a prettier list of activity_ids, titles, and distances
    print(f"WARNING - gear {gid} will be added to all activity id's listed above. Type y to continue")
    cont = input()
    if cont == 'y':         
        for aid in aids:
            if aid:
                add_gear_to_activity(client,aid,gid)



# def upsert_gear_activity(conn: sqlite3.Connection, gear_id: str, data) -> int:
#     THIS DOESN'T WORK - GC NEVER SHOWS ALL THE ACTIVITIES 
#     """Performs an insert of all activiites associated with gear item. 
#     However, this association could be changed retrospectively via the GC website.
#     Therefore, this function refreshes all activities associated with the gear item, regardless of their date
#     """
#     exec_str = f"DELETE FROM activity_gear WHERE gear_id = '{gear_id}';"
#     conn.execute(exec_str)
#     if not data:
#             conn.commit() #we've deleted some stuff so commit req'd
#             return 0
#     count = 0 
#     activity_list = data if isinstance(data,list) else [data]
#     logger.warning(f"fixing gear {gear_id}: {len(activity_list)} entriess") 
#     for i, activity in enumerate(activity_list):   
#         exec_str = """INSERT OR REPLACE INTO activity_gear
#                (activity_id, gear_id)
#                VALUES (?, ?)"""   
#         # exec_str = "INSERT OR REPLACE INTO activity_gear (activity_id,  gear_number) VALUES (?, ?)"   
#         conn.execute(exec_str,
#                 (activity.get('activityId'),
#                 gear_id,
#                 )
#         )
#         count += 1
#     return count