#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import sys
import datetime
import time
import copy
import argparse
import json
import ast
import base64
from functools import wraps
from decimal import Decimal

import util
from util import print_msg, format_satoshis, print_stderr
import bitcoin
from bitcoin import is_address, hash_160, COIN, TYPE_ADDRESS
import transaction
from transaction import Transaction
import paymentrequest
from paymentrequest import PR_PAID, PR_UNPAID, PR_UNKNOWN, PR_EXPIRED
import contacts
known_commands = {}


def satoshis(amount):
    # satoshi conversion must not be performed by the parser
    return int(COIN*Decimal(amount)) if amount not in ['!', None] else amount


class Command:

    def __init__(self, func, s):
        self.name = func.__name__
        self.requires_network = 'n' in s
        self.requires_wallet = 'w' in s
        self.requires_password = 'p' in s
        self.description = func.__doc__
        self.help = self.description.split('.')[0] if self.description else None
        varnames = func.func_code.co_varnames[1:func.func_code.co_argcount]
        self.defaults = func.func_defaults
        if self.defaults:
            n = len(self.defaults)
            self.params = list(varnames[:-n])
            self.options = list(varnames[-n:])
        else:
            self.params = list(varnames)
            self.options = []
            self.defaults = []


def command(s):
    def decorator(func):
        global known_commands
        name = func.__name__
        known_commands[name] = Command(func, s)
        @wraps(func)
        def func_wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return func_wrapper
    return decorator


class Commands:

    def __init__(self, config, wallet, network, callback = None, password=None, new_password=None):
        self.config = config
        self.wallet = wallet
        self.network = network
        self._callback = callback
        self._password = password
        self.new_password = new_password

    def _run(self, method, args, password_getter):
        cmd = known_commands[method]
        if cmd.requires_password and self.wallet.has_password():
            self._password = apply(password_getter,())
            if self._password is None:
                return
        f = getattr(self, method)
        result = f(*args)
        self._password = None
        if self._callback:
            apply(self._callback, ())
        return result

    @command('')
    def commands(self):
        """List of commands"""
        return ' '.join(sorted(known_commands.keys()))

    @command('')
    def create(self):
        """Create a new wallet"""
        raise BaseException('Not a JSON-RPC command')

    @command('wn')
    def restore(self, text):
        """Restore a wallet from text. Text can be a seed phrase, a master
        public key, a master private key, a list of Viacoin addresses
        or Viacoin private keys. If you want to be prompted for your
        seed, type '?' or ':' (concealed) """
        raise BaseException('Not a JSON-RPC command')

    @command('wp')
    def password(self):
        """Change wallet password. """
        self.wallet.update_password(self._password, self.new_password)
        self.wallet.storage.write()
        return {'password':self.wallet.use_encryption}

    @command('')
    def getconfig(self, key):
        """Return a configuration variable. """
        return self.config.get(key)

    @command('')
    def setconfig(self, key, value):
        """Set a configuration variable. 'value' may be a string or a Python expression."""
        try:
            value = ast.literal_eval(value)
        except:
            pass
        self.config.set_key(key, value)
        return True

    @command('')
    def make_seed(self, nbits=132, entropy=1, language=None):
        """Create a seed"""
        from mnemonic import Mnemonic
        s = Mnemonic(language).make_seed('standard', nbits, custom_entropy=entropy)
        return s.encode('utf8')

    @command('')
    def check_seed(self, seed, entropy=1, language=None):
        """Check that a seed was generated with given entropy"""
        from mnemonic import Mnemonic
        return Mnemonic(language).check_seed(seed, entropy)

    @command('n')
    def getaddresshistory(self, address):
        """Return the transaction history of any address. Note: This is a
        walletless server query, results are not checked by SPV.
        """
        return self.network.synchronous_get(('blockchain.address.get_history', [address]))

    @command('w')
    def listunspent(self):
        """List unspent outputs. Returns the list of unspent transaction
        outputs in your wallet."""
        l = copy.deepcopy(self.wallet.get_utxos(exclude_frozen=False))
        for i in l:
            v = i["value"]
            i["value"] = float(v)/COIN if v is not None else None
        return l

    @command('n')
    def getaddressunspent(self, address):
        """Returns the UTXO list of any address. Note: This
        is a walletless server query, results are not checked by SPV.
        """
        return self.network.synchronous_get(('blockchain.address.listunspent', [address]))

    @command('n')
    def getutxoaddress(self, txid, pos):
        """Get the address of a UTXO. Note: This is a walletless server query, results are
        not checked by SPV.
        """
        r = self.network.synchronous_get(('blockchain.utxo.get_address', [txid, pos]))
        return {'address': r}

    @command('')
    def serialize(self, jsontx):
        """Create a transaction from json inputs.
        Inputs must have a redeemPubkey.
        Outputs must be a list of {'address':address, 'value':satoshi_amount}.
        """
        keypairs = {}
        inputs = jsontx.get('inputs')
        outputs = jsontx.get('outputs')
        locktime = jsontx.get('locktime', 0)
        for txin in inputs:
            if txin.get('output'):
                prevout_hash, prevout_n = txin['output'].split(':')
                txin['prevout_n'] = int(prevout_n)
                txin['prevout_hash'] = prevout_hash
            if txin.get('redeemPubkey'):
                pubkey = txin['redeemPubkey']
                txin['type'] = 'p2pkh'
                txin['x_pubkeys'] = [pubkey]
                txin['signatures'] = [None]
                txin['num_sig'] = 1
                if txin.get('privkey'):
                    keypairs[pubkey] = txin['privkey']
            elif txin.get('redeemScript'):
                raise BaseException('Not implemented')

        outputs = map(lambda x: (TYPE_ADDRESS, x['address'], int(x['value'])), outputs)
        tx = Transaction.from_io(inputs, outputs, locktime=locktime)
        tx.sign(keypairs)
        return tx.as_dict()

    @command('wp')
    def signtransaction(self, tx, privkey=None):
        """Sign a transaction. The wallet keys will be used unless a private key is provided."""
        tx = Transaction(tx)
        if privkey:
            pubkey = bitcoin.public_key_from_private_key(privkey)
            h160 = bitcoin.hash_160(pubkey.decode('hex'))
            x_pubkey = 'fd' + (chr(0) + h160).encode('hex')
            tx.sign({x_pubkey:privkey})
        else:
            self.wallet.sign_transaction(tx, self._password)
        return tx.as_dict()

    @command('')
    def deserialize(self, tx):
        """Deserialize a serialized transaction"""
        tx = Transaction(tx)
        return tx.deserialize()

    @command('n')
    def broadcast(self, tx, timeout=30):
        """Broadcast a transaction to the network. """
        tx = Transaction(tx)
        return self.network.broadcast(tx, timeout)

    @command('')
    def createmultisig(self, num, pubkeys):
        """Create multisig address"""
        assert isinstance(pubkeys, list), (type(num), type(pubkeys))
        redeem_script = transaction.multisig_script(pubkeys, num)
        address = bitcoin.hash160_to_p2sh(hash_160(redeem_script.decode('hex')))
        return {'address':address, 'redeemScript':redeem_script}

    @command('w')
    def freeze(self, address):
        """Freeze address. Freeze the funds at one of your wallet\'s addresses"""
        return self.wallet.set_frozen_state([address], True)

    @command('w')
    def unfreeze(self, address):
        """Unfreeze address. Unfreeze the funds at one of your wallet\'s address"""
        return self.wallet.set_frozen_state([address], False)

    @command('wp')
    def getprivatekeys(self, address):
        """Get private keys of addresses. You may pass a single wallet address, or a list of wallet addresses."""
        if is_address(address):
            return self.wallet.get_private_key(address, self._password)
        domain = address
        return [self.wallet.get_private_key(address, self._password) for address in domain]

    @command('w')
    def ismine(self, address):
        """Check if address is in wallet. Return true if and only address is in wallet"""
        return self.wallet.is_mine(address)

    @command('')
    def dumpprivkeys(self):
        """Deprecated."""
        return "This command is deprecated. Use a pipe instead: 'electrum-ltc listaddresses | electrum-ltc getprivatekeys - '"

    @command('')
    def validateaddress(self, address):
        """Check that an address is valid. """
        return is_address(address)

    @command('w')
    def getpubkeys(self, address):
        """Return the public keys for a wallet address. """
        return self.wallet.get_public_keys(address)

    @command('w')
    def getbalance(self):
        """Return the balance of your wallet. """
        c, u, x = self.wallet.get_balance()
        out = {"confirmed": str(Decimal(c)/COIN)}
        if u:
            out["unconfirmed"] = str(Decimal(u)/COIN)
        if x:
            out["unmatured"] = str(Decimal(x)/COIN)
        return out

    @command('n')
    def getaddressbalance(self, address):
        """Return the balance of any address. Note: This is a walletless
        server query, results are not checked by SPV.
        """
        out = self.network.synchronous_get(('blockchain.address.get_balance', [address]))
        out["confirmed"] =  str(Decimal(out["confirmed"])/COIN)
        out["unconfirmed"] =  str(Decimal(out["unconfirmed"])/COIN)
        return out

    @command('n')
    def getproof(self, address):
        """Get Merkle branch of an address in the UTXO set"""
        p = self.network.synchronous_get(('blockchain.address.get_proof', [address]))
        out = []
        for i,s in p:
            out.append(i)
        return out

    @command('n')
    def getmerkle(self, txid, height):
        """Get Merkle branch of a transaction included in a block. Electrum
        uses this to verify transactions (Simple Payment Verification)."""
        return self.network.synchronous_get(('blockchain.transaction.get_merkle', [txid, int(height)]))

    @command('n')
    def getservers(self):
        """Return the list of available servers"""
        return self.network.get_servers()

    @command('')
    def version(self):
        """Return the version of electrum."""
        from version import ELECTRUM_VERSION
        return ELECTRUM_VERSION

    @command('w')
    def getmpk(self):
        """Get master public key. Return your wallet\'s master public key"""
        return self.wallet.get_master_public_key()

    @command('wp')
    def getmasterprivate(self):
        """Get master private key. Return your wallet\'s master private key"""
        return str(self.wallet.keystore.get_master_private_key(self._password))

    @command('wp')
    def getseed(self):
        """Get seed phrase. Print the generation seed of your wallet."""
        s = self.wallet.get_seed(self._password)
        return s.encode('utf8')

    @command('wp')
    def importprivkey(self, privkey):
        """Import a private key. """
        try:
            addr = self.wallet.import_key(privkey, self._password)
            out = "Keypair imported: " + addr
        except BaseException as e:
            out = "Error: " + str(e)
        return out

    def _resolver(self, x):
        if x is None:
            return None
        out = self.wallet.contacts.resolve(x)
        if out.get('type') == 'openalias' and self.nocheck is False and out.get('validated') is False:
            raise BaseException('cannot verify alias', x)
        return out['address']

    @command('nw')
    def sweep(self, privkey, destination, tx_fee=None, nocheck=False, imax=100):
        """Sweep private keys. Returns a transaction that spends UTXOs from
        privkey to a destination address. The transaction is not
        broadcasted."""
        tx_fee = satoshis(tx_fee)
        privkeys = privkey if type(privkey) is list else [privkey]
        self.nocheck = nocheck
        dest = self._resolver(destination)
        tx = self.wallet.sweep(privkeys, self.network, self.config, dest, tx_fee, imax)
        return tx.as_dict() if tx else None

    @command('wp')
    def signmessage(self, address, message):
        """Sign a message with a key. Use quotes if your message contains
        whitespaces"""
        sig = self.wallet.sign_message(address, message, self._password)
        return base64.b64encode(sig)

    @command('')
    def verifymessage(self, address, signature, message):
        """Verify a signature."""
        sig = base64.b64decode(signature)
        return bitcoin.verify_message(address, sig, message)

    def _mktx(self, outputs, fee, change_addr, domain, nocheck, unsigned, rbf):
        self.nocheck = nocheck
        change_addr = self._resolver(change_addr)
        domain = None if domain is None else map(self._resolver, domain)
        final_outputs = []
        for address, amount in outputs:
            address = self._resolver(address)
            amount = satoshis(amount)
            final_outputs.append((TYPE_ADDRESS, address, amount))

        coins = self.wallet.get_spendable_coins(domain)
        tx = self.wallet.make_unsigned_transaction(coins, final_outputs, self.config, fee, change_addr)
        if rbf:
            tx.set_rbf(True)
        if not unsigned:
            self.wallet.sign_transaction(tx, self._password)
        return tx

    @command('wp')
    def payto(self, destination, amount, tx_fee=None, from_addr=None, change_addr=None, nocheck=False, unsigned=False, rbf=False):
        """Create a transaction. """
        tx_fee = satoshis(tx_fee)
        domain = [from_addr] if from_addr else None
        tx = self._mktx([(destination, amount)], tx_fee, change_addr, domain, nocheck, unsigned, rbf)
        return tx.as_dict()

    @command('wp')
    def paytomany(self, outputs, tx_fee=None, from_addr=None, change_addr=None, nocheck=False, unsigned=False, rbf=False):
        """Create a multi-output transaction. """
        tx_fee = satoshis(tx_fee)
        domain = [from_addr] if from_addr else None
        tx = self._mktx(outputs, tx_fee, change_addr, domain, nocheck, unsigned, rbf)
        return tx.as_dict()

    @command('w')
    def history(self):
        """Wallet history. Returns the transaction history of your wallet."""
        balance = 0
        out = []
        for item in self.wallet.get_history():
            tx_hash, height, conf, timestamp, value, balance = item
            if timestamp:
                date = datetime.datetime.fromtimestamp(timestamp).isoformat(' ')[:-3]
            else:
                date = "----"
            label = self.wallet.get_label(tx_hash)
            out.append({
                'txid': tx_hash,
                'timestamp': timestamp,
                'date': date,
                'label': label,
                'value': float(value)/COIN if value is not None else None,
                'height': height,
                'confirmations': conf
            })
        return out

    @command('w')
    def setlabel(self, key, label):
        """Assign a label to an item. Item may be a Viacoin address or a
        transaction ID"""
        self.wallet.set_label(key, label)

    @command('w')
    def listcontacts(self):
        """Show your list of contacts"""
        return self.wallet.contacts

    @command('w')
    def getalias(self, key):
        """Retrieve alias. Lookup in your list of contacts, and for an OpenAlias DNS record."""
        return self.wallet.contacts.resolve(key)

    @command('w')
    def searchcontacts(self, query):
        """Search through contacts, return matching entries. """
        results = {}
        for key, value in self.wallet.contacts.items():
            if query.lower() in key.lower():
                results[key] = value
        return results

    @command('w')
    def listaddresses(self, receiving=False, change=False, show_labels=False, frozen=False, unused=False, funded=False, show_balance=False):
        """List wallet addresses. Returns the list of all addresses in your wallet. Use optional arguments to filter the results."""
        out = []
        for addr in self.wallet.get_addresses():
            if frozen and not self.wallet.is_frozen(addr):
                continue
            if receiving and self.wallet.is_change(addr):
                continue
            if change and not self.wallet.is_change(addr):
                continue
            if unused and self.wallet.is_used(addr):
                continue
            if funded and self.wallet.is_empty(addr):
                continue
            item = addr
            if show_balance:
                item += ", "+ format_satoshis(sum(self.wallet.get_addr_balance(addr)))
            if show_labels:
                item += ', ' + repr(self.wallet.labels.get(addr, ''))
            out.append(item)
        return out

    @command('n')
    def gettransaction(self, txid):
        """Retrieve a transaction. """
        if self.wallet and txid in self.wallet.transactions:
            tx = self.wallet.transactions[txid]
        else:
            raw = self.network.synchronous_get(('blockchain.transaction.get', [txid]))
            if raw:
                tx = Transaction(raw)
            else:
                raise BaseException("Unknown transaction")
        return tx.as_dict()

    @command('')
    def encrypt(self, pubkey, message):
        """Encrypt a message with a public key. Use quotes if the message contains whitespaces."""
        return bitcoin.encrypt_message(message, pubkey)

    @command('wp')
    def decrypt(self, pubkey, encrypted):
        """Decrypt a message encrypted with a public key."""
        return self.wallet.decrypt_message(pubkey, encrypted, self._password)

    def _format_request(self, out):
        pr_str = {
            PR_UNKNOWN: 'Unknown',
            PR_UNPAID: 'Pending',
            PR_PAID: 'Paid',
            PR_EXPIRED: 'Expired',
        }
        out['amount (LTC)'] = format_satoshis(out.get('amount'))
        out['status'] = pr_str[out.get('status', PR_UNKNOWN)]
        return out

    @command('w')
    def getrequest(self, key):
        """Return a payment request"""
        r = self.wallet.get_payment_request(key, self.config)
        if not r:
            raise BaseException("Request not found")
        return self._format_request(r)

    #@command('w')
    #def ackrequest(self, serialized):
    #    """<Not implemented>"""
    #    pass

    @command('w')
    def listrequests(self, pending=False, expired=False, paid=False):
        """List the payment requests you made."""
        out = self.wallet.get_sorted_requests(self.config)
        if pending:
            f = PR_UNPAID
        elif expired:
            f = PR_EXPIRED
        elif paid:
            f = PR_PAID
        else:
            f = None
        if f is not None:
            out = filter(lambda x: x.get('status')==f, out)
        return map(self._format_request, out)

    @command('w')
    def getunusedaddress(self,force=False):
        """Returns the first unused address."""
        addr = self.wallet.get_unused_address()
        if addr is None and force:
            addr = self.wallet.create_new_address(False)

        if addr:
            return addr
        else:
            return False


    @command('w')
    def addrequest(self, amount, memo='', expiration=None, force=False):
        """Create a payment request."""
        addr = self.wallet.get_unused_address()
        if addr is None:
            if force:
                addr = self.wallet.create_new_address(False)
            else:
                return False
        amount = satoshis(amount)
        expiration = int(expiration) if expiration else None
        req = self.wallet.make_payment_request(addr, amount, memo, expiration)
        self.wallet.add_payment_request(req, self.config)
        out = self.wallet.get_payment_request(addr, self.config)
        return self._format_request(out)

    @command('wp')
    def signrequest(self, address):
        "Sign payment request with an OpenAlias"
        alias = self.config.get('alias')
        if not alias:
            raise BaseException('No alias in your configuration')
        alias_addr = self.wallet.contacts.resolve(alias)['address']
        self.wallet.sign_payment_request(address, alias, alias_addr, self._password)

    @command('w')
    def rmrequest(self, address):
        """Remove a payment request"""
        return self.wallet.remove_payment_request(address, self.config)

    @command('w')
    def clearrequests(self):
        """Remove all payment requests"""
        for k in self.wallet.receive_requests.keys():
            self.wallet.remove_payment_request(k, self.config)

    @command('n')
    def notify(self, address, URL):
        """Watch an address. Everytime the address changes, a http POST is sent to the URL."""
        def callback(x):
            import urllib2
            headers = {'content-type':'application/json'}
            data = {'address':address, 'status':x.get('result')}
            try:
                req = urllib2.Request(URL, json.dumps(data), headers)
                response_stream = urllib2.urlopen(req)
                util.print_error('Got Response for %s' % address)
            except BaseException as e:
                util.print_error(str(e))
        self.network.send([('blockchain.address.subscribe', [address])], callback)
        return True

    @command('wn')
    def is_synchronized(self):
        """ return wallet synchronization status """
        return self.wallet.is_up_to_date()

    @command('')
    def help(self):
        # for the python console
        return sorted(known_commands.keys())

param_descriptions = {
    'privkey': 'Private key. Type \'?\' to get a prompt.',
    'destination': 'Viacoin address, contact or alias',
    'address': 'Viacoin address',
    'seed': 'Seed phrase',
    'txid': 'Transaction ID',
    'pos': 'Position',
    'height': 'Block height',
    'tx': 'Serialized transaction (hexadecimal)',
    'key': 'Variable name',
    'pubkey': 'Public key',
    'message': 'Clear text message. Use quotes if it contains spaces.',
    'encrypted': 'Encrypted message',
    'amount': 'Amount to be sent (in LTC). Type \'!\' to send the maximum available.',
    'requested_amount': 'Requested amount (in LTC).',
    'outputs': 'list of ["address", amount]',
}

command_options = {
    'password':    ("-W", "--password",    "Password"),
    'receiving':   (None, "--receiving",   "Show only receiving addresses"),
    'change':      (None, "--change",      "Show only change addresses"),
    'frozen':      (None, "--frozen",      "Show only frozen addresses"),
    'unused':      (None, "--unused",      "Show only unused addresses"),
    'funded':      (None, "--funded",      "Show only funded addresses"),
    'show_balance':("-b", "--balance",     "Show the balances of listed addresses"),
    'show_labels': ("-l", "--labels",      "Show the labels of listed addresses"),
    'nocheck':     (None, "--nocheck",     "Do not verify aliases"),
    'imax':        (None, "--imax",        "Maximum number of inputs"),
    'tx_fee':      ("-f", "--fee",         "Transaction fee (in LTC)"),
    'from_addr':   ("-F", "--from",        "Source address. If it isn't in the wallet, it will ask for the private key unless supplied in the format public_key:private_key. It's not saved in the wallet."),
    'change_addr': ("-c", "--change",      "Change address. Default is a spare address, or the source address if it's not in the wallet"),
    'nbits':       (None, "--nbits",       "Number of bits of entropy"),
    'entropy':     (None, "--entropy",     "Custom entropy"),
    'language':    ("-L", "--lang",        "Default language for wordlist"),
    'gap_limit':   ("-G", "--gap",         "Gap limit"),
    'privkey':     (None, "--privkey",     "Private key. Set to '?' to get a prompt."),
    'unsigned':    ("-u", "--unsigned",    "Do not sign transaction"),
    'rbf':         (None, "--rbf",         "Replace-by-fee transaction"),
    'domain':      ("-D", "--domain",      "List of addresses"),
    'memo':        ("-m", "--memo",        "Description of the request"),
    'expiration':  (None, "--expiration",  "Time in seconds"),
    'timeout':     (None, "--timeout",     "Timeout in seconds"),
    'force':       (None, "--force",       "Create new address beyong gap limit, if no more address is available."),
    'pending':     (None, "--pending",     "Show only pending requests."),
    'expired':     (None, "--expired",     "Show only expired requests."),
    'paid':        (None, "--paid",        "Show only paid requests."),
}


# don't use floats because of rounding errors
from transaction import tx_from_str
json_loads = lambda x: json.loads(x, parse_float=lambda x: str(Decimal(x)))
arg_types = {
    'num': int,
    'nbits': int,
    'imax': int,
    'entropy': long,
    'tx': tx_from_str,
    'pubkeys': json_loads,
    'jsontx': json_loads,
    'inputs': json_loads,
    'outputs': json_loads,
    'tx_fee': lambda x: str(Decimal(x)) if x is not None else None,
    'amount': lambda x: str(Decimal(x)) if x != '!' else '!',
}

config_variables = {

    'addrequest': {
        'requests_dir': 'directory where a bip70 file will be written.',
        'ssl_privkey': 'Path to your SSL private key, needed to sign the request.',
        'ssl_chain': 'Chain of SSL certificates, needed for signed requests. Put your certificate at the top and the root CA at the end',
        'url_rewrite': 'Parameters passed to str.replace(), in order to create the r= part of viacoin: URIs. Example: \"(\'file:///var/www/\',\'https://electrum-ltc.org/\')\"',
    },
    'listrequests':{
        'url_rewrite': 'Parameters passed to str.replace(), in order to create the r= part of viacoin: URIs. Example: \"(\'file:///var/www/\',\'https://electrum-ltc.org/\')\"',
    }
}

def set_default_subparser(self, name, args=None):
    """see http://stackoverflow.com/questions/5176691/argparse-how-to-specify-a-default-subcommand"""
    subparser_found = False
    for arg in sys.argv[1:]:
        if arg in ['-h', '--help']:  # global help if no subparser
            break
    else:
        for x in self._subparsers._actions:
            if not isinstance(x, argparse._SubParsersAction):
                continue
            for sp_name in x._name_parser_map.keys():
                if sp_name in sys.argv[1:]:
                    subparser_found = True
        if not subparser_found:
            # insert default in first position, this implies no
            # global options without a sub_parsers specified
            if args is None:
                sys.argv.insert(1, name)
            else:
                args.insert(0, name)

argparse.ArgumentParser.set_default_subparser = set_default_subparser


# workaround https://bugs.python.org/issue23058
# see https://github.com/nickstenning/honcho/pull/121

def subparser_call(self, parser, namespace, values, option_string=None):
    from argparse import ArgumentError, SUPPRESS, _UNRECOGNIZED_ARGS_ATTR
    parser_name = values[0]
    arg_strings = values[1:]
    # set the parser name if requested
    if self.dest is not SUPPRESS:
        setattr(namespace, self.dest, parser_name)
    # select the parser
    try:
        parser = self._name_parser_map[parser_name]
    except KeyError:
        tup = parser_name, ', '.join(self._name_parser_map)
        msg = _('unknown parser %r (choices: %s)') % tup
        raise ArgumentError(self, msg)
    # parse all the remaining options into the namespace
    # store any unrecognized options on the object, so that the top
    # level parser can decide what to do with them
    namespace, arg_strings = parser.parse_known_args(arg_strings, namespace)
    if arg_strings:
        vars(namespace).setdefault(_UNRECOGNIZED_ARGS_ATTR, [])
        getattr(namespace, _UNRECOGNIZED_ARGS_ATTR).extend(arg_strings)

argparse._SubParsersAction.__call__ = subparser_call


def add_network_options(parser):
    parser.add_argument("-1", "--oneserver", action="store_true", dest="oneserver", default=False, help="connect to one server only")
    parser.add_argument("-s", "--server", dest="server", default=None, help="set server host:port:protocol, where protocol is either t (tcp) or s (ssl)")
    parser.add_argument("-p", "--proxy", dest="proxy", default=None, help="set proxy [type:]host[:port], where type is socks4,socks5 or http")

def add_global_options(parser):
    group = parser.add_argument_group('global options')
    group.add_argument("-v", "--verbose", action="store_true", dest="verbose", default=False, help="Show debugging information")
    group.add_argument("-D", "--dir", dest="electrum_path", help="electrum directory")
    group.add_argument("-P", "--portable", action="store_true", dest="portable", default=False, help="Use local 'vialectrum_data' directory")
    group.add_argument("-w", "--wallet", dest="wallet_path", help="wallet path")
    group.add_argument("--testnet", action="store_true", dest="testnet", default=False, help="Use Testnet")
    group.add_argument("--segwit", action="store_true", dest="segwit", default=False, help="The Wizard will create Segwit seed phrases (Testnet only).")
    group.add_argument("--nolnet", action="store_true", dest="nolnet", default=False, help="Use Nolnet")

def get_parser():
    # create main parser
    parser = argparse.ArgumentParser(
        epilog="Run 'electrum-ltc help <command>' to see the help for a command")
    add_global_options(parser)
    subparsers = parser.add_subparsers(dest='cmd', metavar='<command>')
    # gui
    parser_gui = subparsers.add_parser('gui', description="Run Electrum's Graphical User Interface.", help="Run GUI (default)")
    parser_gui.add_argument("url", nargs='?', default=None, help="viacoin URI (or bip70 file)")
    parser_gui.add_argument("-g", "--gui", dest="gui", help="select graphical user interface", choices=['qt', 'kivy', 'text', 'stdio'])
    parser_gui.add_argument("-o", "--offline", action="store_true", dest="offline", default=False, help="Run offline")
    parser_gui.add_argument("-m", action="store_true", dest="hide_gui", default=False, help="hide GUI on startup")
    parser_gui.add_argument("-L", "--lang", dest="language", default=None, help="default language used in GUI")
    add_network_options(parser_gui)
    add_global_options(parser_gui)
    # daemon
    parser_daemon = subparsers.add_parser('daemon', help="Run Daemon")
    parser_daemon.add_argument("subcommand", choices=['start', 'status', 'stop', 'load_wallet', 'close_wallet'], nargs='?')
    #parser_daemon.set_defaults(func=run_daemon)
    add_network_options(parser_daemon)
    add_global_options(parser_daemon)
    # commands
    for cmdname in sorted(known_commands.keys()):
        cmd = known_commands[cmdname]
        p = subparsers.add_parser(cmdname, help=cmd.help, description=cmd.description)
        add_global_options(p)
        if cmdname == 'restore':
            p.add_argument("-o", "--offline", action="store_true", dest="offline", default=False, help="Run offline")
        #p.set_defaults(func=run_cmdline)
        if cmd.requires_password:
            p.add_argument("-W", "--password", dest="password", default=None, help="password")
        for optname, default in zip(cmd.options, cmd.defaults):
            a, b, help = command_options[optname]
            action = "store_true" if type(default) is bool else 'store'
            args = (a, b) if a else (b,)
            if action == 'store':
                _type = arg_types.get(optname, str)
                p.add_argument(*args, dest=optname, action=action, default=default, help=help, type=_type)
            else:
                p.add_argument(*args, dest=optname, action=action, default=default, help=help)

        for param in cmd.params:
            h = param_descriptions.get(param, '')
            _type = arg_types.get(param, str)
            p.add_argument(param, help=h, type=_type)

        cvh = config_variables.get(cmdname)
        if cvh:
            group = p.add_argument_group('configuration variables', '(set with setconfig/getconfig)')
            for k, v in cvh.items():
                group.add_argument(k, nargs='?', help=v)

    # 'gui' is the default command
    parser.set_default_subparser('gui')
    return parser
