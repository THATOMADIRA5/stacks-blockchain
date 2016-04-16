#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstack
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

    This file is part of Blockstack

    Blockstack is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import json
import sys
import urllib2
import stat
import time

from ..config import *
from ..nameset import *
from .auth import *
from .monitor import *

from ..scripts import is_name_valid

import blockstack_client

import virtualchain
log = virtualchain.get_logger("blockstack-server")


def get_cached_zonefile( zonefile_hash, zonefile_dir=None ):
    """
    Get a cached zonefile from local disk
    Return None if not found
    """
    if zonefile_dir is None:
        zonefile_dir = get_zonefile_dir()

    zonefile_path = os.path.join( zonefile_dir, zonefile_hash )
    with open(zonefile_path, "r") as f:
        data = f.read()

    # sanity check 
    if not verify_zonefile( data, zonefile_hash ):
        log.debug("Corrupt zonefile '%s'; uncaching" % zonefile_hash)
        remove_cached_zonefile( zonefile_hash, zonefile_dir=zonefile_dir )
        return None

    return data


def get_zonefile_from_storage( zonefile_hash ):
    """
    Get a zonefile from our storage drivers.
    Return the zonefile dict on success.
    Raise on error
    """
    
    if not is_current_zonefile_hash( zonefile_hash ):
        raise Exception("Unknown zonefile hash")

    data = blockstack_client.storage.get_immutable_data( zonefile_hash, hash_func=blockstack_client.get_blockchain_compat_hash )
    if 'error' in data:
        raise Exception("Failed to get data: %s" % data['error'])

    zonefile_data = data['data']

    # verify 
    if hash_zonefile( zonefile_data ) != zonefile_hash:
        raise Exception("Corrupt zonefile: %s" % zonefile_hash)
    
    return zonefile_data


def get_zonefile_from_peers( zonefile_hash, peers ):
    """
    Get a zonefile from a peer Blockstack node.
    Try the sequence of peers (as (host, port) tuples), by asking
    for the zonefile via RPC
    Return a zonefile that matches the given hash on success.
    Return None if no zonefile could be obtained.
    """

    zonefile_data = None 

    for (host, port) in peers:

        blockstack_rpc = blockstack_client.session(server_host=host, server_port=port)
        rpc = MonitoredRPCClient( blockstack_rpc )

        zonefile_data = rpc.get_zonefiles( [zonefile_hash] )
        if 'error' in zonefile_data:
            # next peer 
            log.error("Peer %s:%s: %s" % (host, port, zonefile_data['error']) )
            zonefile_data = None
            continue

        if not zonefile_data['zonefiles'].has_key(zonefile_hash):
            # nope
            log.error("Peer %s:%s did not return %s" % zonefile_hash)
            zonefile_data = None
            continue

        # extract zonefile
        zonefile_data = zonefile_data['zonefiles'][zonefile_hash]

        # verify zonefile
        h = hash_zonefile( zonefile_data )
        if h != zonefile_hash:
            log.error("Zonefile hash mismatch: expected %s, got %s" % (zonefile_hash, h))
            zonefile_data = None
            continue

        # success!
        break

    return zonefile_data


def store_cached_zonefile( zonefile_dict, zonefile_dir=None ):
    """
    Store a validated zonefile.
    zonefile_data should be a dict.
    The caller should first authenticate the zonefile.
    Return True on success
    Return False on error
    """
    if zonefile_dir is None:
        zonefile_dir = get_zonefile_dir()

    zonefile_data = json.dumps(zonefile_dict, sort_keys=True)
    zonefile_hash = blockstack_client.get_name_zonefile_hash( zonefile_data )
    zonefile_path = os.path.join( zonefile_dir, zonefile_hash )
        
    try:
        with open( zonefile_path, "w" ) as f:
            f.write(zonefile_data)
            f.flush()
            os.fsync(f.fileno())
    except Exception, e:
        log.exception(e)
        return False
        
    return True


def get_zonefile_txid( zonefile_dict ):
    """
    Look up the transaction ID of the transaction
    that wrote this zonefile.
    Return the txid on success
    Return None on error
    """
    
    zonefile_txt = serialize_zonefile( zonefile_dict )
    zonefile_hash = hash_zonefile( zonefile_txt )
    name = zonefile_dict.get('$origin')
    if name is None:
        log.debug("Missing '$origin' in zonefile")
        return None

    # must be a valid name 
    if not is_name_valid( name ):
        log.debug("Invalid name in zonefile")
        return None

    db = get_db_state()

    # what's the associated transaction ID?
    txid = db.get_name_value_hash_txid( name, zonefile_hash )
    if txid is None:
        log.debug("No txid for zonefile hash '%s' (for '%s')" % (zonefile_hash, name))
        return None

    return txid


def store_zonefile_to_storage( zonefile_dict ):
    """
    Upload a zonefile to our storage providers.
    Return True if at least one provider got it.
    Return False otherwise.
    """
    zonefile_txt = serialize_zonefile( zonefile_dict )
    zonefile_hash = hash_zonefile( zonefile_txt )
    
    if not is_current_zonefile_hash( zonefile_hash ):
        log.error("Unknown zonefile %s" % zonefile_hash)
        return False

    # find the tx that paid for this zonefile
    txid = get_zonefile_txid( zonefile_dict )
    if txid is None:
        log.error("No txid for zonefile hash '%s' (for '%s')" % (zonefile_hash, name))
        return False
    
    rc = blockstack_client.storage.put_immutable_data( None, txid, data_hash=zonefile_hash, data_text=zonefile_text )
    if not rc:
        log.error("Failed to store zonefile '%s' (%s) for '%s'" (zonefile_hash, txid, name))
        return False

    return True


def remove_cached_zonefile( zonefile_hash, zonefile_dir=None ):
    """
    Remove a zonefile from the local cache.
    """
    if zonefile_dir is None:
        zonefile_dir = get_zonefile_dir()

    path = os.path.join( zonefile_dir, zonefile_hash )
    try:
        os.unlink(path)
        return True
    except:
        return False


def remove_zonefile_from_storage( zonefile_dict, wallet_keys=None ):
    """
    Remove a zonefile from external storage
    Return True on success
    Return False on error
    """
    zonefile_txt = serialize_zonefile( zonefile_dict )
    zonefile_hash = hash_zonefile( zonefile_txt )

    if not is_current_zonefile_hash( zonefile_hash ):
        log.error("Unknown zonefile %s" % zonefile_hash)
        return False

    # find the tx that paid for this zonefile
    txid = get_zonefile_txid( zonefile_dict )
    if txid is None:
        log.error("No txid for zonefile hash '%s' (for '%s')" % (zonefile_hash, name))
        return False
    
    _, data_privkey = blockstack_client.get_data_keypair( wallet_keys=wallet_keys )
    rc = blockstack_client.storage.delete_immutable_data( zonefile_hash, txid, data_privkey )
    if not rc:
        return False

    return True


def clean_cached_zonefile_dir( zonefile_dir=None ):
    """
    Clean out stale entries in the zonefile directory.
    """
    if zonefile_dir is None:
        zonefile_dir = get_zonefile_dir()

    db = get_db_state()
    hashes = os.listdir( zonefile_dir )
    for h in hashes:
        if h in ['.', '..']:
            continue 

        remove_zonefile( h, zonefile_dir=zonefile_dir )

    return



