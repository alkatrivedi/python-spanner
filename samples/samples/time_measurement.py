import argparse
import base64
import datetime
import decimal
import json
import logging
import time

from google.cloud import spanner
from google.cloud.spanner_admin_instance_v1.types import spanner_instance_admin
from google.cloud.spanner_v1 import param_types
from google.type import expr_pb2
from google.iam.v1 import policy_pb2
from google.cloud.spanner_v1.data_types import JsonObject
from google.protobuf import field_mask_pb2  # type: ignore

OPERATION_TIMEOUT_SECONDS = 240

from threading import Thread, Lock
import random
import string
import uuid
import _thread
import threading


# [START spanner_query_data]
def query_data(instance_id, database_id):
    """Queries sample data from the database using SQL."""
    spanner_client = spanner.Client()
    instance = spanner_client.instance(instance_id)
    database = instance.database(database_id)

    with database.snapshot() as snapshot:
        results = snapshot.execute_sql(
            "SELECT SingerId, AlbumId, AlbumTitle FROM Albums"
        )

        for row in results:
            print("SingerId: {}, AlbumId: {}, AlbumTitle: {}".format(*row))



def insert_with_dml(instance_id, database_id, i):
    """Inserts data with a DML statement into the database."""
    # [START spanner_dml_getting_started_insert]
    # instance_id = "your-spanner-instance"
    # database_id = "your-spanner-db-id"

    start = time.time()


    spanner_client = spanner.Client()
    instance = spanner_client.instance(instance_id)
    database = instance.database(database_id, close_inactive_transaction = True)


    def insert_singers(transaction):
        row_ct = transaction.execute_update(
            
            # first_name = str(uuid.uuid4())
            # last_name = str(uuid.uuid4())

            # Generate a random integer between a and b (inclusive)
            # a = 1
            # b = 100

            # random_integer = random.randint(a, b)
            "INSERT Singers (SingerId, FirstName, LastName) VALUES ({}, 'Virginia{}', 'Watson{}')".format(i, i, i)
            # "INSERT INTO Singers (SingerId, FirstName, LastName) VALUES "
            # "(12, 'Melissa', 'Garcia'), "
            # "(13, 'Russell', 'Morales'), "
            # "(14, 'Jacqueline', 'Long'), "
            # "(15, 'Dylan', 'Shaw')"
        )
        print("{} record(s) inserted.".format(row_ct))

        print(
            "\n"
        )

    database.run_in_transaction(insert_singers)

    end = time.time()

    time_taken = end - start

    print("time taken: ", time_taken)

    with open ('output.txt', 'a') as file:  
        file.write(str(time_taken)+"\n")  
    # [END spanner_dml_getting_started_insert]


if __name__ == "__main__":  # noqa: C901
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("instance_id", help="Your Cloud Spanner instance ID.")
    parser.add_argument(
        "--database-id", help="Your Cloud Spanner database ID.", default="example_db"
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("insert_with_dml", help=insert_with_dml.__doc__)
    subparsers.add_parser("query_data", help=query_data.__doc__)
    # enable_fine_grained_access_parser.add_argument("--title", default="condition title")

    args = parser.parse_args()
    if args.command == "insert_with_dml":

        # insert_with_dml(args.instance_id, args.database_id)


        start = 11
        end = 20
        threads = []
        for i in range(start, end + 1):
            t = threading.Thread(target=insert_with_dml, args=(args.instance_id, args.database_id, i,))
            t.start()
            threads.append(t)
            # _thread.start_new_thread(insert_with_dml,(args.instance_id, args.database_id, i,))

        # Wait for all threads to complete
        for t in threads:
            t.join()
            
        # for prefix in 'dsfsdfsfdsfsdfs':
        #     _thread.start_new_thread(insert_with_dml,(args.instance_id, args.database_id, prefix,))
    elif args.command == "query_data":
        query_data(args.instance_id, args.database_id)
