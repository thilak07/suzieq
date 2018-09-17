
from collections import defaultdict, Counter
import asyncio
import random
import time
import re
import ast
import json
import copy

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import textfsm


def exdict(path, data, start, collect=False):
    '''Extract all fields in specified path from data'''

    def set_kv(okeys, indata, oresult):
        '''Set the value in the outgoing dict'''
        if '?' in okeys[1]:
            fkey, fvals = okeys[1].split('?')
            fvals = fvals.split('|')
            cval = indata.get(okeys[0], '')
            if not cval or (fvals[0] and cval != fvals[0]):
                if fvals[1].isdigit():
                    oresult[fkey.strip()] = int(fvals[1])
                else:
                    oresult[fkey.strip()] = fvals[1]
            else:
                oresult[fkey.strip()] = indata.get(okeys[0], '')
        else:
            oresult[okeys[1].strip()] = indata.get(okeys[0], '')

    result = []
    iresult = {}

    plist = re.split('''/(?=(?:[^'"]|'[^']*'|"[^"]*")*$)''', path)
    for i, elem in enumerate(plist[start:]):

        if not data:
            return result, i+start

        j = 0
        num = re.match('\[([0-9]*)\]', elem)
        if num:
            data = data[int(num.group(1))]

        elif elem.startswith('*'):
            use_key = False
            is_list = True
            if type(data) is dict:
                is_list = False
                okeys = elem.split(':')
                if len(okeys) > 1:
                    use_key = True

            for item in data:
                if not is_list:
                    datum = data[item]
                else:
                    datum = item
                if type(datum) is dict or type(datum) is list:
                    if use_key:
                        if collect:
                            # We're at the leaf and just gathering the fields
                            # as a comma separated string
                            # example: memberInterfaces/* : lacpMembers
                            cstr = ''
                            for key in data:
                                cstr = ', ' + key + cstr if cstr else key
                            iresult[okeys[1].strip()] = item
                            result.append(iresult)
                            return result, i + start
                        iresult[okeys[1].strip()] = item
                    tmpres, j = exdict(path, datum, start+i+1)
                    if tmpres:
                        for subresult in tmpres:
                            iresult.update(subresult)
                            result.append(iresult)
                            iresult = {}
                else:
                    continue
            if j >= i:
                break
        elif type(elem) is str:
            # split the normalized key and data key
            okeys = elem.split(':')
            if okeys[0] in data:
                if i+1 == len(plist):
                    set_kv(okeys, data, iresult)
                    result.append(iresult)
                else:
                    data = data[okeys[0]]
            else:
                try:
                    fields = ast.literal_eval(elem)
                except (ValueError, SyntaxError):
                    # Catch if the elem is a string key not present in the data
                    # Case of missing key, abort if necessary
                    if not path.endswith(']') and ':' in plist[-1]:
                        okeys = plist[-1].split(':')
                        set_kv(okeys, data, iresult)
                        result.append(iresult)
                        return result, i+start

                if type(fields) is list:
                    for fld in fields:
                        if '/' in fld:
                            sresult, _ = exdict(fld, data, 0, collect=True)
                            for res in sresult:
                                if '+' in fld:
                                    wkey = fld.split(':')[-1].strip()[1:]
                                    if wkey in iresult:
                                        for k, v in res.items():
                                            iresult[wkey] += v
                                    else:
                                        iresult.update(res)
                                else:
                                    iresult.update(res)
                        else:
                            okeys = fld.split(':')
                            set_kv(okeys, data, iresult)
                    result.append(iresult)
                    iresult = {}

    return result, i+start


def equal_dicts(a, b, ignore_keys):
    '''Compare list of dictionaries ignoreing certain keys
    '''
    ka = []
    kb = []
    for i in a:
        vals = [v for k, v in i.items() if k not in ignore_keys]
        ka.append(tuple(str(vals)))

    for i in b:
        vals = [v for k, v in i.items() if k not in ignore_keys]
        kb.append(tuple(str(vals)))

    return Counter(ka) == Counter(kb)


def munge_interfaces_result(result, devtype):
    '''Homogenize the IP addresses across different implementations'''
    if devtype == 'eos':
        new_list = []
        if result:
            munge_entry = result.get('IPAddresses', [[]])[0]
            if munge_entry:
                primary_ip = (
                    munge_entry['primaryIp']['address'] + '/' +
                    str(munge_entry['primaryIp']['maskLen'])
                )
                new_list.append(primary_ip)
                for entry in munge_entry['secondaryIpsOrderedList']:
                    ip = entry['adddress'] + '/' + entry['maskLen']
                    new_list.append(ip)

            munge_entry = result.get('IP6Addresses', [[]])
            if munge_entry:
                for entry in munge_entry[0].get('globalUnicastIp6s', []):
                    new_list.append(entry['subnet'])

            result['IPAddresses'] = new_list
            del result['IP6Addresses']

    return


def textfsm_data(raw_input, fsm_template):
    '''Convert unstructured output to structured output'''

    records = []
    try:
        template = open(fsm_template)
    except IOError as e:
        logging.error('Unable to open textfsm template {}, error:{}'.format(
            fsm_template, e))
        return records

    re_table = textfsm.TextFSM(template)
    res = re_table.ParseText(raw_input)

    for entry in res:
        metent = dict(zip(re_table.header, entry))
        records.append(metent)

    return records


class Service(object):
    name = None
    defn = None
    period = 15                 # 15s is the default period
    update_nodes = False
    output_dir = None
    nodes = {}
    new_nodes = {}
    ignore_keys = []

    def __init__(self, name, defn, ignore_keys, output_dir):
        self.name = name
        self.defn = defn
        self.ignore_keys = ignore_keys
        self.output_dir = output_dir

    def set_nodes(self, nodes):
        if self.nodes:
            self.new_nodes = copy.deepcopy(nodes)
            update_nodes = True
        else:
            self.nodes = copy.deepcopy(nodes)

    async def gather_data(self):
        raise NotImplementedError

    def process_data(self, data):

        result = []
        if data['status'] == 200 or data['status'] == 0:
            nfn = self.defn.get(data.get('hostname'), None)
            if not nfn:
                nfn = self.defn.get(data.get('devtype'), None)
            if nfn:
                if nfn.get('normalize', None):
                    if type(data['data']) is str:
                        input = json.loads(data['data'])
                    else:
                        input = data['data']

                    result, _ = exdict(nfn.get('normalize', ''), input, 0)
                else:
                    tfsm_template = nfn.get('textfsm', '')
                    result = textfsm_data(data['data'], tfsm_template)

                if self.name == 'interfaces':
                    for entry in result:
                        # Sadly ip addresses are given in so many diff ways
                        munge_interfaces_result(entry,
                                                data.get('devtype', None))
                        # Check that there has been a real change

        return result

    async def commit_data(self, result, datacenter, hostname, timestamp):
        '''Write the result data out'''
        records = []
        if result:
            prev_res = self.nodes.get(hostname, '').prev_result
            if not equal_dicts(prev_res, result, self.ignore_keys):
                self.nodes.get(hostname, '') \
                          .prev_result = copy.deepcopy(result)
                for entry in result:
                    entry.update({'hostname': hostname})
                    entry.update({'timestamp': timestamp})
                    records.append(entry)

            if records:
                df = pd.DataFrame.from_dict(records, dtype=str)
                table = pa.Table.from_pandas(df)
                pq.write_to_dataset(
                    table,
                    root_path='{}/{}/{}'.format(self.output_dir,
                                                datacenter,
                                                self.name),
                    partition_cols=['timestamp'])

    async def run(self):
        '''Start the service'''
        while True:
            keys = list(self.nodes.keys())
            random.shuffle(keys)

            tasks = [self.nodes[key].exec_service(self.defn)
                     for key in keys if self.nodes[key].is_alive()]

            outputs = await asyncio.gather(*tasks)
            for output in outputs:
                result = self.process_data(output[0])
                await self.commit_data(result, output[0]['datacenter'],
                                       output[0]['hostname'],
                                       output[0]['timestamp'])

            if self.update_nodes:
                for node in self.new_nodes:
                    # Copy the last saved outputs to avoid committing dup data
                    if node in self.nodes:
                        new_nodes[node]['output'] = nodes[node]['output']

                nodes = new_nodes
                new_nodes = []
                update_nodes = False

            await asyncio.sleep(self.period)

