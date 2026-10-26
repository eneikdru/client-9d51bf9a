"""Trusted operator CLI. End includes any required buffer; UTC epoch seconds."""
import argparse
from app import connection

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('id')
parser.add_argument('master')
parser.add_argument('service')
parser.add_argument('start', type=int)
parser.add_argument('end', type=int)
parser.add_argument('price', type=int, help='GEL minor units')
args = parser.parse_args()
with connection() as db:
    db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?)', (args.id, args.master, args.service, args.start, args.end, args.price))
