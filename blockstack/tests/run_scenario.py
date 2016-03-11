#!/usr/bin/env python 

import os
import sys 
import subprocess
import signal
import shutil
import time
import atexit

# enable all tests
os.environ['BLOCKSTACK_TEST'] = '1'

# hack around absolute paths 
current_dir =  os.path.abspath(os.path.dirname(__file__) + "/../..")
sys.path.insert(0, current_dir)

import pybitcoin
from blockstack.lib import *
from blockstack.tests import *
import virtualchain 
import importlib
import traceback

from blockstack.lib import nameset as blockstack_state_engine

import blockstack.tests.mock_bitcoind as mock_bitcoind

import blockstack
import blockstack.blockstackd as blockstackd

import scenarios.testlib as testlib

log = virtualchain.get_logger()

mock_bitcoind_connection = None
api_server = None

def atexit_kill_api_server():
    if api_server is not None:
        try:
            api_server.kill()
            api_server.wait()
        except:
            pass

def load_scenario( scenario_name ):
    """
    Load up a scenario, and validate it.
    A scenario is a python file with:
    * a global variable 'wallet' that is a dict
    which maps private keys to their initial values.
    * a global variable 'consensus' that represents 
    the initial consensus hash.
    * a callable called 'scenario' that takes the 
    wallet as an argument and runs the test.
    * a callable called 'check' that takes the state 
    engine as an argument and checks it for correctness.
    """

    # strip .py from scenario name 
    if scenario_name.endswith(".py"):
        scenario_name = scenario_name[:-3]

    try:
        scenario = importlib.import_module( scenario_name )
    except ImportError, ie:
        raise Exception("Failed to import '%s'." % scenario_name )

    # validate 
    if not hasattr( scenario, "wallets" ):
        # default empty wallet 
        log.warning("Empty wallet for scenario '%s'" % scenario_name )
        scenario.wallets = {}

    if not hasattr( scenario, "consensus" ):
        # default consensus hash 
        log.warning("No consensus hash for '%s'" % scenario_name )
        scenario.consensus = "00" * 16

    if not hasattr( scenario, "scenario" ):
        # not a valid test 
        log.error("Invalid scenario '%s': no 'scenario' method" % scenario_name )
        return None 

    if not hasattr( scenario, "check" ):
        # not a valid test 
        log.error("Invalid scenario '%s': no 'check' method" % scenario_name )
        return None 

    return scenario
   

def write_config_file( scenario, path ):
    """
    Generate the config file to use with this test scenario.
    Write it to path.
    """

    initial_utxo_str = ",".join( ["%s:%s" % (w.privkey, w.value) for w in scenario.wallets] )
    config_file_in = "blockstack-server.ini.in"
    mock_bitcoind_save_path = "/tmp/mock_bitcoind.dat"

    config_txt = None
    with open( config_file_in, "r" ) as f:
        config_txt = f.read()

    config_txt = config_txt.replace( "@MOCK_INITIAL_UTXOS@", initial_utxo_str )
    config_txt = config_txt.replace( "@MOCK_SAVE_FILE@", mock_bitcoind_save_path )

    with open( path, "w" ) as f:
        f.write( config_txt )
        f.flush()

    return 0


def run_scenario( scenario, config_file ):
    """
    Run a test scenario:
    * set up the virtualchain to use our mock UTXO provider and mock bitcoin blockchain
    * seed it with the initial values in the wallet 
    * set the initial consensus hash 
    * start the API server
    * run the scenario method
    * run the check method
    """

    global api_server
    atexit.register( atexit_kill_api_server )

    mock_bitcoind_save_path = "/tmp/mock_bitcoind.dat"
    if os.path.exists( mock_bitcoind_save_path ):
        try:
            os.unlink(mock_bitcoind_save_path)
        except:
            pass

    # use mock bitcoind
    worker_env = mock_bitcoind.make_worker_env( mock_bitcoind, mock_bitcoind_save_path )
    worker_env['BLOCKSTACK_TEST'] = "1"

    print worker_env

    # tell our subprocesses that we're testing 
    os.environ.update( worker_env )

    if os.environ.get("PYTHONPATH", None) is not None:
        worker_env["PYTHONPATH"] = os.environ["PYTHONPATH"]

    # virtualchain defaults...
    virtualchain.setup_virtualchain( impl=blockstack_state_engine, bitcoind_connection_factory=mock_bitcoind.connect_mock_bitcoind, index_worker_env=worker_env )

    # set up blockstack
    # NOTE: utxo_opts encodes the mock-bitcoind options 
    blockstack_opts, bitcoin_opts, utxo_opts, dht_opts = blockstack.lib.configure( config_file=config_file, interactive=False )
   
    # override multiprocessing options to ensure single-process behavior 
    utxo_opts['multiprocessing_num_procs'] = 1 
    utxo_opts['multiprocessing_num_blocks'] = 10

    # pass along extra arguments
    utxo_opts['save_file'] = mock_bitcoind_save_path

    print ""
    print "blockstack opts"
    print json.dumps( blockstack_opts, indent=4 )

    print ""
    print "bitcoin opts"
    print json.dumps( bitcoin_opts, indent=4 )

    print ""
    print "UTXO opts"
    print json.dumps( utxo_opts, indent=4 )

    print ""
    print "DHT opts"
    print json.dumps( dht_opts, indent=4 )

    # save headers as well 
    utxo_opts['spv_headers_path'] = mock_bitcoind_save_path + ".spvheaders"
    with open( utxo_opts['spv_headers_path'], "w" ) as f:
        # write out "initial" headers, up to the first block
        empty_header = ("00" * 81).decode('hex')
        for i in xrange(0, blockstack.FIRST_BLOCK_MAINNET ): 
            f.write( empty_header )

    blockstackd.set_bitcoin_opts( bitcoin_opts )
    blockstackd.set_utxo_opts( utxo_opts )

    # start API server 
    api_server = blockstackd.api_server_subprocess( foreground=True )

    db = blockstackd.get_db_state()
    bitcoind = mock_bitcoind.connect_mock_bitcoind( utxo_opts )
    sync_virtualchain_upcall = lambda: virtualchain.sync_virtualchain( utxo_opts, bitcoind.getblockcount(), db )
    mock_utxo = blockstack.lib.connect_utxo_provider( utxo_opts )
    working_dir = virtualchain.get_working_dir()
 
    # set up test environment
    testlib.set_utxo_opts( utxo_opts )
    testlib.set_utxo_client( mock_utxo )
    testlib.set_bitcoind( bitcoind )
    testlib.set_state_engine( db )

    test_env = {
        "sync_virtualchain_upcall": sync_virtualchain_upcall,
        "working_dir": working_dir,
        "bitcoind": bitcoind,
        "bitcoind_save_path": mock_bitcoind_save_path
    }

    # sync initial utxos 
    testlib.next_block( **test_env )

    try:
        os.unlink( mock_bitcoind_save_path )
    except:
        pass

    # load the scenario into the mock blockchain and mock utxo provider
    try:
        scenario.scenario( scenario.wallets, **test_env )

    except Exception, e:
        log.exception(e)
        traceback.print_exc()
        log.error("Failed to run scenario '%s'" % scenario.__name__)

        api_server.send_signal( signal.SIGTERM )
        api_server.wait()
        api_server = None
        return False

    # run the checks on the database
    try:
        rc = scenario.check( db )
    except Exception, e:
        log.exception(e)
        traceback.print_exc()
        log.error("Failed to run tests '%s'" % scenario.__name__)
        
        api_server.send_signal( signal.SIGTERM )
        api_server.wait()
        api_server = None
        return False 
    
    if not rc:
        api_server.send_signal( signal.SIGTERM )
        api_server.wait()
        api_server = None
        return rc

    log.info("Scenario checks passed; verifying history")

    # run database integrity check at each block 
    rc = testlib.check_history( db )
    if not rc:
        api_server.send_signal( signal.SIGTERM )
        api_server.wait()
        api_server = None
        return rc

    log.info("History check passes!")

    # run snv at each name 
    rc = testlib.snv_all_names( db )
    if not rc:
        api_server.send_signal( signal.SIGTERM )
        api_server.wait()
        api_server = None
        return rc

    log.info("SNV check passes!")
    api_server.send_signal( signal.SIGTERM )
    api_server.wait()
    api_server = None
    return rc 


if __name__ == "__main__":

    if len(sys.argv) < 2:
        print >> sys.stderr, "Usage: %s [scenario.import.path] [OPTIONAL: working dir]"
        sys.exit(1)
 
    # load up the scenario 
    scenario = load_scenario( sys.argv[1] )
    if scenario is None:
        print "Failed to load '%s'" % sys.argv[1]
        sys.exit(1)

    working_dir = None
    if len(sys.argv) > 2:
        working_dir = sys.argv[2]
    else:
        working_dir = "/tmp/blockstack-run-scenario.%s" % scenario.__name__

    # patch state engine implementation
    os.environ['BLOCKSTACK_TEST_WORKING_DIR'] = working_dir     # for the API server
    blockstack_state_engine.working_dir = working_dir   # for virtualchain
    if not os.path.exists( blockstack_state_engine.working_dir ):
        os.makedirs( blockstack_state_engine.working_dir )

    # generate config file
    config_file = os.path.join( blockstack_state_engine.working_dir, "blockstack-server.ini" ) 
    rc = write_config_file( scenario, config_file )
    if rc != 0:
        log.error("failed to write config file: exit %s" % rc)
        sys.exit(1)

    # run the test 
    rc = run_scenario( scenario, config_file )
   
    if rc:
        print "SUCCESS %s" % scenario.__name__
        # shutil.rmtree( working_dir )
        sys.exit(0)
    else:
        print >> sys.stderr, "FAILURE %s" % scenario.__name__
        print >> sys.stderr, "Test output in %s" % working_dir
        sys.exit(1)

    
