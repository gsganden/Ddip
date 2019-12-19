import sys, time, os, re, ipyparallel, torch, atexit, IPython, dataclasses, subprocess, signal
from collections import OrderedDict
from typing import Iterable
from torch.distributed import *
from IPython.core.magic import Magics, magics_class, cell_magic, line_magic
from IPython.core.display import clear_output
from fastai.distributed import *
from fastai.torch_core import *
from fastai import basic_data, basic_train, core, text
from fastai.basic_train import Learner
import fastprogress
from ipyparallel import AsyncResult
from fastprogress.fastprogress import master_bar, progress_bar, force_console_behavior, IN_NOTEBOOK, isnotebook

__all__ = ['IppDdp', 'IppCluster', 'Ddp', 'DDP_Apps', 'FastaiDDP']

Debug = True
Verbose = True

def _debug(*args, **kwargs):
    if Debug: print(*args, file=sys.stderr, **kwargs)

class IppCluster():
    "Start/stop of an ipyparallel cluster aka 'ipcluster, and access to cluster engines."
    cid = "ippdpp_c"
    def __init__(self, n:int=0, engine_wait:float=15.0, **kwargs):
        "Launch the ipyparallel cluster using 'ipcluster start', then connect via Client()"
        popen_cmd = ["ipcluster", "start", f"--cluster-id={IppCluster.cid}"]
        if n > 0: popen_cmd.append(f"--n={n}")

        self._proc = subprocess.Popen(popen_cmd) # Ignore stdout and stderr from ipcluster
        # Connect clients/views to engines, and load iPython cell magics
        try: self.client = ipyparallel.Client(timeout=engine_wait,cluster_id=f"{IppCluster.cid}")
        except (IOError,TimeoutError) as e: raise Exception(f"ipyparallel Client() failed: {e}")

        while engine_wait > 0:
            try:
                self.px_view = self.client[:]
                engine_wait = 0
            except ipyparallel.error.NoEnginesRegistered as e:
                Verbose and print(f"Waiting for ipyparallel cluster to be ready .", file=sys.stderr)
                time.sleep(4)
                engine_wait -= 4

    def __del__(self): self.shutdown()

    def shutdown(self, *args, **kwargs):
        if self.client:
            subprocess.call(["ipcluster", "stop", f"--cluster-id={IppCluster.cid}"])
            if self._proc and self._proc.poll() == None:
                try: self._proc.wait(10)
                except subprocess.TimeoutExpired as e: self._proc.send_signal(signal.SIGTERM)
            self.px_view = self.client = self._proc = None
            Verbose and print(f"IppCluster.shutdown(): Cluster shut down.", file=sys.stderr) 
        
'''
FastAI specific setup
'''
class FastaiDDP():
    _original_Learner_pi_:callable = None

    @staticmethod
    def _distributed_Learner_post_init(learner):
        FastaiDDP._original_Learner_pi_(learner)
        learner.to_distributed(torch.cuda.current_device())

    @staticmethod
    def _distrib_Learner(switch_on:bool):
        if switch_on:
            if FastaiDDP._original_Learner_pi_ is None:  # Intercept Learner.__post_init__() to append our own handler
                FastaiDDP._original_Learner_pi_ = Learner.__post_init__
                Learner.__post_init__ = FastaiDDP._distributed_Learner_post_init
        else:
            if FastaiDDP._original_Learner_pi_ is not None:
                Learner.__post_init__ = FastaiDDP._original_Learner_pi_

    @staticmethod    
    def silent_console(silent:bool=True):
        "Turn off console progress bar output."
        fastprogress.fastprogress.NO_BAR = silent
        mbar, pbar = force_console_behavior()
        cls_list = [fastprogress.fastprogress, basic_train, basic_data,
            dataclasses.dataclass, text, text.data, core]
        for c in cls_list: c.master_bar, c.progress_bar = mbar,pbar

    @staticmethod
    def fastai_init_ddp(*args, **kwargs):
        '''Do a few housekeeping:
        0. Set defaults.device to the proper device, due to a bug in fastai.torch_core.py
           where the defaults.device is initialized without regard to the curren cuda device.
        1. Limit standard output to only the RANK 0 process.
        3. Intercept Learner constructor to call to_distributed() after Learner.__post_init__().
        '''
        print(f"Entering fastai_init_ddp(): {os.getpid()}", flush=True)
        if torch.cuda.is_available(): torch.cuda.set_device(torch.cuda.current_device()) # work-around for the fastai.torch_core.defaults.device maybe out of sync
        silence = rank_distrib() != 0
        FastaiDDP.silent_console(silence) # limit console output to only rank 0 GPU
        FastaiDDP._distrib_Learner(switch_on=True) # Update Learner post-initializer

    @staticmethod
    def fastai_finalize_ddp(*args, **kwargs):
        FastaiDDP.silent_console(False) # Let them sing again
        FastaiDDP._distrib_Learner(switch_on=False) # Restore Learner post-initializer

DDP_Apps = {
    'fastai' : { 
        'imports' : [ 'import fastprogress', 'from ippdpp.ipp_distrib import *', 'from fastai.distributed import *', 'import torch'],
        'initializer' : FastaiDDP.fastai_init_ddp, 'finalizer' : FastaiDDP.fastai_finalize_ddp,
        'been_here' : False, }
}

DEFAULT_APP='fastai'

# PyTorch distributed data parallel (DDP) setup
class Ddp():
    def __init__(self, **kwargs):
        assert torch.cuda.is_available(), "CUDA not available! (Try reloading cuda driver?)"
        n_engines=torch.cuda.device_count()
        self.cluster = IppCluster(n=n_engines, **kwargs)
        self.ddp_group = None

    def __del__(self): self.shutdown_cluster()

    @staticmethod
    def join_group_single(g_rank:int, l_rank:int, gpu:int, ws:int):
        import os, torch
        os.environ["RANK"] = str(g_rank) # Global rank
        os.environ["LOCAL_RANK"] = str(l_rank) # Local rank
        os.environ["MASTER_ADDR"] = "127.0.0.1"
        os.environ["MASTER_PORT"] = str(29500)
        os.environ["WORLD_SIZE"] = str(ws)
        os.environ["OMP_NUM_THREADS"] = str(1) # See https://github.com/pytorch/pytorch/pull/22501
        torch.cuda.set_device(gpu)
        if ws > 1: torch.distributed.init_process_group(backend='nccl', init_method='env://')

    def new_group(self, gpus:List[int], node_rank=0, world_size=0):
        '''Configure a torch distributed process group of GPUs over a ipyparallel cluster.
        Returns a list of GPU ids of the group formed.'''
        n_gpu = len(gpus)
        assert n_gpu <= len(self.cluster.client), f"More GPU ({gpus}) than ipyparallel engines ({len(self.cluster.client)})"
        assert max(gpus) < torch.cuda.device_count(), f"Invalid GPU id {max(gpus)}, highest allowed is {torch.cuda.device_count()-1}"

        Verbose and print(f"Initializing torch distributed group with GPUs {gpus}", flush=True)

        if world_size==0: world_size = n_gpu
        for rank,gpu in enumerate(gpus):
            dist_rank = n_gpu * node_rank + rank # see torch/distributed/launch.py
            self.cluster.client[rank].push(dict(g_rank=dist_rank, l_rank=rank, gpu=gpu, ws=world_size))
            self.cluster.client[rank].execute('from ippdpp.ipp_distrib import *')

        self.cluster.client[0:n_gpu].execute('Ddp.join_group_single(g_rank=g_rank, l_rank=l_rank, gpu=gpu, ws=ws)')
        self.cluster.px_view = self.cluster.client[0:n_gpu]
        self.ddp_group = gpus

    def exit_group(self):
        if self.ddp_group is None: return
        Verbose and print(f"DDP.exit_group(): {self.ddp_group}", flush=True)
        self.cluster.client[0:len(self.ddp_group)].execute('torch.distributed.destroy_process_group()',block=True)
        self.cluster.px_view = self.ddp_group = None

    def shutdown_cluster(self):
        if self.cluster: self.cluster.shutdown()
        self.ddp_group = self.cluster = None

from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring

@magics_class
class IppDdp(Magics):
    "An helper object to execution on an ipyparallel cluster, one engine per GPU."
    _instance = None # A singleton
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(IppDdp,cls).__new__(cls,*args,**kwargs)
        return cls._instance

    def __init__(self, shell:IPython.InteractiveShell=None, **kwargs):
        super(IppDdp, self).__init__(shell=shell) # will setup self.shell
        self._autoddp = None # Flag to control if parallel execution is by default ON or OFF
        self._in_autoddp = False
        self._app = None
        self._default_output_pause=0.1
        self._px_targets = None # comma-delimited list of ipcluster engines for parallel execution
        self.ddp = Ddp(**kwargs) # Controller for DDP, and the underlying ipyparallel cluster

    def __del__(self): IppDdp.close()

    @classmethod
    def close(cls):
        if cls._instance: cls._instance.ddp.shutdown_cluster()
        cls._instance = None

    class StreamPrinter():
        ''' Each invocation prints from where it left off till the end of the current output '''
        def __init__(self, streams, *args, **kwargs):
            self._counts = [0] * len(streams)
        def __call__(self, streams, *args, **kwargs):
            for i, st in enumerate(streams):
                if (len(st) > self._counts[i]):
                    print(st[self._counts[i]:], end='', flush=True)
                    self._counts[i] = len(st)
    
    def app_init(self, appname:str):
        app = DDP_Apps.get(appname, None)
        if app is None: raise ValueError(f"Unknown app '{appname}' for Torch DDP.  Available ones are: {DDP_Apps.keys()}")
            
        if not app['been_here']:
            dv = self.ddp.cluster.px_view # shorthand for the DDP process group
            for imp in app['imports']: dv.execute(imp, block=True)
            dv.apply_sync(app['initializer'])
            app['been_here'] = True
            self._app = app
            print(f"Initialized ipyparallel extension for {appname}")

    def app_exit(self):
        if self._app and self._app['been_here']:
            dv = self.ddp.cluster.px_view
            dv.apply_sync(self._app['finalizer'])
            self._app['been_here'] = False
            self._app = None
           
    def prepender(self, lines:List[str]):
        '''Prepend a magic marker, then a designated magic to a code block. Thanks to the example at:
            https://github.com/jdanbrown/potoo/blob/master/potoo/default_magic_magic.py
        '''
        # Support filtering, by regex or function hooks? A functor class?
        if lines and self._autoddp:
            if not lines[0].startswith('%'): lines.insert(0, f'%%{self._autoddp}\n')
        return lines
        
     # Add autoddp --freemem:bool
     # move to a separate general purpose magic class?
    @line_magic
    def autoddp(self, line:str):
        if line:
            hooks = self.shell.input_transformers_cleanup
            if line.split(None,1)[0] == "off": # Unregister the prepender
                self._autoddp = None
                while self.prepender in hooks: hooks.remove(self.prepender)
            else: # Register the prepender, 
                self._autoddp = line
                if self.prepender not in hooks: hooks.append(self.prepender)
        print(f"Auto parallel execution: {self._autoddp if self._autoddp else 'Off'}")
        return self._autoddp

    @magic_arguments()  # TODO: --gpus to accept list of integers
    @argument('-g', '--gpus', dest='gpus', type=str, nargs='+', help="comma or space seperated list of GPU ids, or 'all' to specify all GPUs available.")
    @argument('-a', '--app', dest='appname', type=str, default='fastai')
    @argument('-d', '--debug', nargs='?', type=lambda x: (str(x).lower() == 'true'))
    @argument('-v', '--verbose', nargs='?', type=lambda x: (str(x).lower() == 'true'))
    @line_magic
    def ddprep(self, line=''):
        "%ddprep -- line magic to setup/tear down the cluster as a DDP training group, app-specific handling of object"
        if self.shell is None: raise RuntimeError("%%ddpx: Not in an ipython Interactive shell!")
        args = parse_argstring(self.ddprep, line)

        global Debug # Monkey hack until  classes have their own module namespaces and own debug/verbose flags
        if Debug != args.debug: Debug = args.debug
        global Verbose
        if Verbose != args.verbose: Verbose = args.verbose

        if args.gpus:
            if 'all' in args.gpus: gpus = list(range(torch.cuda.device_count()))
            else: gpus = list(OrderedDict.fromkeys([ int(g) for g in re.split(',\s*|\s+', ','.join(args.gpus))]))

            if self.ddp.ddp_group == gpus:
                print(f"%ddprep DDP group unchanged, GPUs ids: {gpus}")
                return  # same group or empty group => do nothing
            
            if self.ddp.ddp_group: # In reverse order: clean up the app first, then the DDP group
                self.app_exit()
                self._px_targets = None
                self.ddp.exit_group()

            self.ddp.new_group(gpus=gpus)
            self._px_targets = f"--targets {','.join(map(str, range(len(gpus))))}"
            self.app_init(args.appname)
        
 
    @magic_arguments()
    @argument('--quiet', dest='quiet', action='store_true', help="Display any stdout only after task is finished, skip all the transient, real-time output.")
    # @argument('watch', type=str, nargs='?', default='', help='Print progress output from asynchronous calls.')
    @cell_magic
    def ddpx(self, line, cell): # CAN WATCH BE CONTEXT SENSITIVE?
        "%%ddpx - Parallel execution on cluster, allows transient output be displayed"
        if self.shell is None: raise RuntimeError("%%ddpx: Not in an ipython Interactive shell!")
        Verbose and print(f"Invoking cell magic: %%ddpx {line}", file=sys.stderr, flush=True)
        if not self.ddp.ddp_group: self.ddprep()
        args = parse_argstring(self.ddpx, line)

        px_args=f"--noblock {self._px_targets}"
        ar = self.shell.run_cell_magic("px", px_args, cell) # use parallel_execute?

        watcher = self._app.get('watcher', IppDdp.StreamPrinter)(ar.stdout) if (not args.quiet) and self._app else None

        if watcher:
            while not ar.ready(): # Simulate wait on blocking execution
                watcher(ar.stdout)
                time.sleep(self._default_output_pause)
            watcher(ar.stdout)

        r = ar.get() # Blocks
        ar.display_outputs()

        return r

def unload_ipython_extension(ipython):
    IppDdp.close()

def load_ipython_extension(ipython:IPython.InteractiveShell):
    __iddpM = IppDdp(ipython)
    atexit.register(unload_ipython_extension, ipython)
    ipython.push({'ddpmagic':__iddpM, 'ddp':__iddpM.ddp, 'ddprc':__iddpM.ddp.cluster.client})
    ipython.register_magics(__iddpM)
