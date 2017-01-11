import boto3
import botocore

import json
import base64
import cPickle as pickle
import wrenconfig
import wrenutil
import enum
from multiprocessing.pool import ThreadPool
import time
import s3util
import logging
import botocore
import glob2
import os
import numpy as np
from cloudpickle import serialize

logger = logging.getLogger(__name__)

class JobState(enum.Enum):
    new = 1
    invoked = 2
    running = 3
    success = 4
    error = 5

def default_executor():
    config = wrenconfig.default()
    AWS_REGION = config['account']['aws_region']
    FUNCTION_NAME = config['lambda']['function_name']
    S3_BUCKET = config['s3']['bucket']
    S3_PREFIX = config['s3']['pywren_prefix']
    return Executor(AWS_REGION, S3_BUCKET, S3_PREFIX, FUNCTION_NAME, config)

class Executor(object):
    """
    Theoretically will allow for cross-AZ invocations
    """

    def __init__(self, aws_region, s3_bucket, s3_prefix, function_name, 
                 config):
        self.aws_region = aws_region
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.config = config
        self.lambda_function_name = function_name

        self.session = botocore.session.get_session()
        self.lambclient = self.session.create_client('lambda', 
                                                     region_name = aws_region)
        self.s3client = self.session.create_client('s3', region_name = aws_region)
        


    def create_mod_data(self, mod_paths):

        module_data = {}
        # load mod paths
        for m in mod_paths:
            if os.path.isdir(m):
                files = glob2.glob(os.path.join(m, "**/*.py"))
                pkg_root = os.path.dirname(m)
            else:
                pkg_root = os.path.dirname(m)
                files = [m]
            for f in files:
                dest_filename = f[len(pkg_root)+1:]

                module_data[f[len(pkg_root)+1:]] = open(f, 'r').read()

        return module_data

    def put_data(self, s3_data_key, data_str, 
                 callset_id, call_id):

        # put on s3 -- FIXME right now this takes 2x as long 
        
        self.s3client.put_object(Bucket = s3_data_key[0], 
                                 Key = s3_data_key[1], 
                                 Body = data_str)

        logger.info("call_async {} {} s3 upload complete {}".format(callset_id, call_id, s3_data_key))


    def invoke_with_keys(self, s3_func_key, s3_data_key, s3_output_key, 
                         s3_status_key, 
                         callset_id, call_id, extra_env, 
                         extra_meta, data_byte_range, use_cached_runtime, 
                         host_job_meta):
    
        arg_dict = {'func_key' : s3_func_key, 
                    'data_key' : s3_data_key, 
                    'output_key' : s3_output_key, 
                    'status_key' : s3_status_key, 
                    'callset_id': callset_id, 
                    'data_byte_range' : data_byte_range, 
                    'call_id' : call_id, 
                    'use_cached_runtime' : use_cached_runtime, 
                    'runtime_s3_bucket' : self.config['runtime']['s3_bucket'], 
                    'runtime_s3_key' : self.config['runtime']['s3_key']}    

        if extra_env is not None:
            arg_dict['extra_env'] = extra_env

        if extra_meta is not None:
            # sanity 
            for k, v in extra_meta.iteritems():
                if k in arg_dict:
                    raise ValueError("Key {} already in dict".format(k))
                arg_dict[k] = v

        host_submit_time = time.time()
        arg_dict['host_submit_time'] = host_submit_time

        json_arg = json.dumps(arg_dict)

        logger.info("call_async {} {} lambda invoke ".format(callset_id, call_id))
        lambda_invoke_time_start = time.time()
        res = self.lambclient.invoke(FunctionName=self.lambda_function_name, 
                                     Payload = json.dumps(arg_dict), 
                                     InvocationType='Event')
        host_job_meta['lambda_invoke_timestamp'] = lambda_invoke_time_start
        host_job_meta['lambda_invoke_time'] = time.time() - lambda_invoke_time_start


        logger.info("call_async {} {} lambda invoke complete".format(callset_id, call_id))


        host_job_meta.update(arg_dict)

        fut = ResponseFuture(call_id, callset_id, self, host_job_meta)

        fut._set_state(JobState.invoked)

        return fut
        
    def call_async(self, func, data, extra_env = None, 
                    extra_meta=None):
        return self.map(func, [data], extra_meta, extra_env)[0]

    def agg_data(self, data_strs):
        ranges = []
        pos = 0
        for datum in data_strs:
            l = len(datum)
            ranges.append((pos, pos + l -1))
            pos += l
        return "".join(data_strs), ranges

    def map(self, func, iterdata, extra_env = None, extra_meta = None, 
            invoke_pool_threads=64, data_all_as_one=True, 
            use_cached_runtime=True):
        """
        # FIXME work with an actual iterable instead of just a list

        data_all_as_one : upload the data as a single s3 object; fewer
        tcp transactions (good) but potentially higher latency for workers (bad)

        use_cached_runtime : if runtime has been cached, use that. When set
        to False, redownloads runtime.
        """

        pool = ThreadPool(invoke_pool_threads)
        callset_id = s3util.create_callset_id()
        data = list(iterdata)

        ### pickle func and all data (to capture module dependencies
        serializer = serialize.SerializeIndependent()
        func_and_data_ser, mod_paths = serializer([func] + data)
        
        func_str = func_and_data_ser[0]
        data_strs = func_and_data_ser[1:]
        data_size_bytes = np.sum(len(x) for x in data_strs)
        s3_agg_data_key = None
        host_job_meta = {'aggregated_data_in_s3' : False, 
                         'data_size_bytes' : data_size_bytes}
        
        if data_size_bytes < wrenconfig.MAX_AGG_DATA_SIZE and data_all_as_one:
            s3_agg_data_key = s3util.create_agg_data_key(self.s3_bucket, 
                                                      self.s3_prefix, callset_id)
            agg_data_bytes, agg_data_ranges = self.agg_data(data_strs)
            agg_upload_time = time.time()
            self.s3client.put_object(Bucket = s3_agg_data_key[0], 
                                     Key = s3_agg_data_key[1], 
                                     Body = agg_data_bytes)
            host_job_meta['agg_data_in_s3'] = True
            host_job_meta['data_upload_time'] = time.time() - agg_upload_time
        else:
            # FIXME add warning that you wanted data all as one but 
            # it exceeded max data size 
            pass
            

        module_data = self.create_mod_data(mod_paths)

        ### Create func and upload 
        func_module_str = pickle.dumps({'func' : func_str, 
                                        'module_data' : module_data}, -1)

        s3_func_key = s3util.create_func_key(self.s3_bucket, self.s3_prefix, 
                                             callset_id)
        self.s3client.put_object(Bucket = s3_func_key[0], 
                                 Key = s3_func_key[1], 
                                 Body = func_module_str)

        def invoke(data_str, callset_id, call_id, s3_func_key, 
                   host_job_meta, 
                   s3_agg_data_key = None, data_byte_range=None ):
            s3_data_key, s3_output_key, s3_status_key \
                = s3util.create_keys(self.s3_bucket,
                                     self.s3_prefix, 
                                     callset_id, call_id)
            
            if s3_agg_data_key is None:
                data_upload_time = time.time()
                self.put_data(s3_data_key, data_str, 
                              callset_id, call_id)
                data_upload_time = time.time() - data_upload_time
                host_job_meta['data_upload_time'] = data_upload_time

                data_key = s3_data_key
            else:
                data_key = s3_agg_data_key

            return self.invoke_with_keys(s3_func_key, data_key, 
                                         s3_output_key, 
                                         s3_status_key, 
                                         callset_id, call_id, extra_env, 
                                         extra_meta, data_byte_range, 
                                         use_cached_runtime, {})

        N = len(data)
        call_result_objs = []
        for i in range(N):
            call_id = "{:05d}".format(i)

            data_byte_range = None
            if s3_agg_data_key is not None:
                data_byte_range = agg_data_ranges[i]

            cb = pool.apply_async(invoke, (data_strs[i], callset_id, 
                                           call_id, s3_func_key, 
                                           host_job_meta.copy(), 
                                           s3_agg_data_key, 
                                           data_byte_range))

            logger.info("map {} {} apply async".format(callset_id, call_id))

            call_result_objs.append(cb)

        res =  [c.get() for c in call_result_objs]
        pool.close()
        pool.join()
        logger.info("map invoked {} {} pool join".format(callset_id, call_id))

        # FIXME take advantage of the callset to return a lot of these 

        # note these are just the invocation futures

        return res
    
    
    
def get_call_status(callset_id, call_id, 
                    AWS_S3_BUCKET = wrenconfig.AWS_S3_BUCKET, 
                    AWS_S3_PREFIX = wrenconfig.AWS_S3_PREFIX, 
                    AWS_REGION = wrenconfig.AWS_REGION, s3=None):
    s3_data_key, s3_output_key, s3_status_key = s3util.create_keys(AWS_S3_BUCKET, 
                                                                    AWS_S3_PREFIX, 
                                                                    callset_id, call_id)
    if s3 is None:
        s3 = global_s3_client
    
    try:
        r = s3.get_object(Bucket = s3_status_key[0], Key = s3_status_key[1])
        result_json = r['Body'].read()
        return json.loads(result_json)
    
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == "NoSuchKey":
            return None
        else:
            raise e


def get_call_output(callset_id, call_id,
                    AWS_S3_BUCKET = wrenconfig.AWS_S3_BUCKET, 
                    AWS_S3_PREFIX = wrenconfig.AWS_S3_PREFIX, 
                    AWS_REGION = wrenconfig.AWS_REGION, s3=None):
    s3_data_key, s3_output_key, s3_status_key = s3util.create_keys(AWS_S3_BUCKET, 
                                                                    AWS_S3_PREFIX, 
                                                                    callset_id, call_id)
    
    if s3 is None:
        s3 = global_s3_client # boto3.client('s3', region_name = AWS_REGION)

    r = s3.get_object(Bucket = s3_output_key[0], Key = s3_output_key[1])
    return pickle.loads(r['Body'].read())
    

class ResponseFuture(object):

    """
    """
    GET_RESULT_SLEEP_SECS = 4
    def __init__(self, call_id, callset_id, executor, invoke_metadata):

        self.call_id = call_id
        self.callset_id = callset_id 
        self._state = JobState.new
        self.executor = executor
        self._invoke_metadata = invoke_metadata.copy()
        
        self.status_query_count = 0
        
    def _set_state(self, new_state):
        ## FIXME add state machine
        self._state = new_state

    def cancel(self):
        raise NotImplementedError("Cannot cancel dispatched jobs")

    def cancelled(self):
        raise NotImplementedError("Cannot cancel dispatched jobs")

    def running(self):
        raise NotImplementedError()
        
    def done(self):
        if self._state in [JobState.success, JobState.error]:
            return True
        if self.result(check_only = True) is None:
            return False
        return True


    def result(self, timeout=None, check_only=False, throw_except=True):
        """


        From the python docs:

        Return the value returned by the call. If the call hasn't yet
        completed then this method will wait up to timeout seconds. If
        the call hasn't completed in timeout seconds then a
        TimeoutError will be raised. timeout can be an int or float.If
        timeout is not specified or None then there is no limit to the
        wait time.
        
        If the future is cancelled before completing then CancelledError will be raised.
        
        If the call raised then this method will raise the same exception.

        """
        if self._state == JobState.new:
            raise ValueError("job not yet invoked")
        
        if self._state == JobState.success:
            return self._return_val
            
        if self._state == JobState.error:
            raise self._exception

        
        call_status = get_call_status(self.callset_id, self.call_id, 
                                      AWS_S3_BUCKET = self.executor.s3_bucket, 
                                      AWS_S3_PREFIX = self.executor.s3_prefix, 
                                      AWS_REGION = self.executor.aws_region, 
                                      s3 = self.executor.s3client)
        self.status_query_count += 1

        ## FIXME implement timeout
        if timeout is not None : raise NotImplementedError()

        if check_only is True:
            if call_status is None:
                return None

        while call_status is None:
            time.sleep(self.GET_RESULT_SLEEP_SECS)
            call_status = get_call_status(self.callset_id, self.call_id, 
                                          AWS_S3_BUCKET = self.executor.s3_bucket, 
                                          AWS_S3_PREFIX = self.executor.s3_prefix, 
                                          AWS_REGION = self.executor.aws_region, 
                                          s3 = self.executor.s3client)
            self.status_query_count += 1

        self._invoke_metadata['status_query_count'] = self.status_query_count
            
        # FIXME check if it actually worked all the way through 
        
        call_output_time = time.time()
        call_invoker_result = get_call_output(self.callset_id, self.call_id, 
                                              AWS_S3_BUCKET = self.executor.s3_bucket, 
                                              AWS_S3_PREFIX = self.executor.s3_prefix,
                                              AWS_REGION = self.executor.aws_region, 
                                              s3 = self.executor.s3client)
        call_output_time_done = time.time()
        self._invoke_metadata['download_output_time'] = call_output_time_done - call_output_time_done
        

        call_success = call_invoker_result['success']
        logger.info("ResponseFuture.result() {} {} call_success {}".format(self.callset_id, 
                                                                           self.call_id, 
                                                                           call_success))
        


        self._call_invoker_result = call_invoker_result

        if call_success:
            
            self._return_val = call_invoker_result['result']
            self._state = JobState.success
        else:
            self._exception = call_invoker_result['result']
            self._state = JobState.error


        self.run_status = call_status # this is the remote status information
        self.invoke_status = self._invoke_metadata # local status information

        if call_success:
            return self._return_val
        elif call_success == False and throw_except:
            raise self._exception
        return None
            
    def exception(self, timeout = None):
        raise NotImplementedError()

    def add_done_callback(self, fn):
        raise NotImplementedError()

    



ALL_COMPLETED = 1
ANY_COMPLETED = 2
ALWAYS = 3

def wait(fs, return_when=ALL_COMPLETED, THREADPOOL_SIZE=64, 
         WAIT_DUR_SEC=5):
    """
    this will eventually provide an optimization for checking if a large
    number of futures have completed without too much network traffic
    by exploiting the callset
    
    From python docs:
    
    Wait for the Future instances (possibly created by different Executor
    instances) given by fs to complete. Returns a named 2-tuple of
    sets. The first set, named "done", contains the futures that completed
    (finished or were cancelled) before the wait completed. The second
    set, named "not_done", contains uncompleted futures.


    http://pythonhosted.org/futures/#concurrent.futures.wait

    """
    N = len(fs)

    if return_when==ALL_COMPLETED:
        result_count = 0
        while result_count < N:

            fs_dones, fs_notdones = _wait(fs, THREADPOOL_SIZE)
            result_count = len(fs_dones)

            if result_count == N:
                return fs_dones, fs_notdones
            else:
                time.sleep(WAIT_DUR_SEC)

    elif return_when == ANY_COMPLETED:
        raise NotImplementedError()
    elif return_when == ALWAYS:
        return _wait(fs, THREADPOOL_SIZE)
    else:
        raise ValueError()

def _wait(fs, THREADPOOL_SIZE):
    """
    internal function that performs the majority of the WAIT task
    work. 
    """


    # get all the futures that are not yet done
    not_done_futures =  [f for f in fs if f._state not in [JobState.success, 
                                                       JobState.error]]

    # check if the not-done ones have the same callset_id
    present_callsets = set([f.callset_id for f in not_done_futures])
    if len(present_callsets) > 1:
        raise NotImplementedError()

    # get the list of all objects in this callset
    callset_id = present_callsets.pop() # FIXME assume only one
    f0 = not_done_futures[0] # This is a hack too 

    callids_done = s3util.get_callset_done(f0.executor.s3_bucket, 
                                           f0.executor.s3_prefix,
                                           callset_id)
    callids_done = set(callids_done)

    fs_dones = []
    fs_notdones = []

    f_to_wait_on = []
    for f in fs:
        if f._state in [JobState.success, JobState.error]:
            # done, don't need to do anything
            fs_dones.append(f)
        else:
            if f.call_id in callids_done:
                f_to_wait_on.append(f)
                fs_dones.append(f)
            else:
                fs_notdones.append(f)
    def test(f):
        f.result(throw_except=False)
    pool = ThreadPool(THREADPOOL_SIZE)
    pool.map(test, f_to_wait_on)

    pool.close()
    pool.join()

    return fs_dones, fs_notdones

    
def log_test():
    logger.info("logging from pywren.wren")
