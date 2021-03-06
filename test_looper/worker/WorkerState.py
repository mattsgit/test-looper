import collections
import errno
import json
import logging
import os
import shutil
import signal
import simplejson
import sys
import tarfile
import threading
import time
import requests
import traceback
import subprocess
import base64
import fnmatch
import tempfile
import cStringIO as StringIO
import uuid

for name in ["boto3", "requests", "urllib"]:
    logging.getLogger(name).setLevel(logging.CRITICAL)

import test_looper.core.SubprocessRunner as SubprocessRunner
import test_looper.core.tools.Git as Git

if sys.platform != "win32":
    import docker
    import test_looper.core.tools.Docker as Docker
    import test_looper.core.tools.DockerWatcher as DockerWatcher
else:
    docker = None
    Docker = None
    DockerWatcher = None

import test_looper.data_model.TestDefinition as TestDefinition
import test_looper

def withTime(logger):
    def logWithTime(msg, *args):
        if args:
            msg = msg % args

        msg = time.asctime() + " TestLooper> " + msg + ("\n" if msg[-1:] != "\n" else "")
        logger(msg)
    return logWithTime

        

class DummyWorkerCallbacks:
    def __init__(self, localTerminal=False):
        self.logMessages = []
        self.artifacts = []
        self.localTerminal = localTerminal

    def heartbeat(self, logMessage=None):
        if logMessage is not None:
            self.logMessages.append(logMessage)

    def recordArtifactUploaded(self, artifact):
        self.artifacts.append(artifact)

    def terminalOutput(self, output):
        pass

    def subscribeToTerminalInput(self, callback):
        pass

    def scopedReadLockAroundGitRepo(self):
        class Scope:
            def __enter__(self, *args):
                pass
            def __exit__(self, *args):
                pass
        return Scope()

HEARTBEAT_INTERVAL=3

PASSTHROUGH_KEYS = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]

EARLY_STOP = "EARLY_STOP"

class NAKED_MACHINE:
    pass

class TestLooperDirectories:
    def __init__(self, worker_directory):
        self.repo_cache = os.path.join(worker_directory, "repos")
        self.repo_copy_dir = os.path.join(worker_directory, "src")
        self.scratch_dir = os.path.join(worker_directory, "scratch_dir")
        self.command_dir = os.path.join(worker_directory, "command")
        self.test_inputs_dir = os.path.join(worker_directory, "test_inputs")
        self.test_output_dir = os.path.join(worker_directory, "test_output")
        self.build_output_dir = os.path.join(worker_directory, "build_output")
        self.test_data_dir = os.path.join(worker_directory, "test_data")
        self.build_cache_dir = os.path.join(worker_directory, "build_cache")
        self.ccache_dir = os.path.join(worker_directory, "ccache")
        self.worker_directory = worker_directory

    def all(self):
        return [self.repo_copy_dir, self.scratch_dir, self.command_dir, self.test_inputs_dir, self.test_data_dir, 
                self.build_cache_dir, self.ccache_dir, self.test_output_dir, self.build_output_dir, self.repo_cache]

class WorkerState(object):
    def __init__(self, name_prefix, worker_directory, source_control, artifactStorage, machineId, hardwareConfig, verbose=False, docker_image_repo=None):
        import test_looper.worker.TestLooperWorker

        self.name_prefix = name_prefix

        assert isinstance(worker_directory, (str,unicode)), worker_directory
        worker_directory = str(worker_directory)

        self.worker_directory = worker_directory

        self.verbose = verbose

        self.directories = TestLooperDirectories(worker_directory)

        self.repos_by_name = {}

        self.machineId = machineId

        self.hardwareConfig = hardwareConfig

        for path in self.directories.all():
            self.ensureDirectoryExists(path)

        self.max_build_cache_depth = 10

        self.artifactStorage = artifactStorage

        self.source_control = source_control

        self.docker_image_repo = docker_image_repo

        self.cleanup()

    def callHeartbeatInBackground(self, log_function, logMessage=None):
        if logMessage is not None:
            log_function(time.asctime() + " TestLooper> " + logMessage + "\n")

        stop = threading.Event()
        receivedException = [None]

        def heartbeatThread():
            while not stop.is_set():
                stop.wait(10)
                if stop.is_set():
                    return
                else:
                    try:
                        log_function("")
                    except Exception as e:
                        stop.set()
                        receivedException[0] = e

        loggingThread = threading.Thread(target=heartbeatThread)

        class Scope:
            def __enter__(scope):
                loggingThread.start()

            def __exit__(scope, exc_type, exc_value, traceback):
                stop.set()
                loggingThread.join()

                if receivedException[0] is not None:
                    if exc_value is not None:
                        logging.error("Got exception %s but also got a heartbeat exception." % exc_value)
                    raise receivedException[0]

        return Scope()
    
    def getRepoCacheByName(self, name):
        if name not in self.repos_by_name:
            self.repos_by_name[name] = Git.Git(str(os.path.join(self.directories.repo_cache, name)))

            if not self.repos_by_name[name].isInitialized():
                self.repos_by_name[name].cloneFrom(self.source_control.getRepo(name).cloneUrl())

        return self.repos_by_name[name]

    def cleanup(self):
        if Docker is not None:
            Docker.DockerImage.removeDanglingDockerImages()
            Docker.killAllWithNamePrefix(self.name_prefix)

        self.clearDirectoryAsRoot(
            self.directories.test_data_dir, 
            self.directories.test_output_dir,
            self.directories.build_output_dir,
            self.directories.scratch_dir, 
            self.directories.test_inputs_dir, 
            self.directories.command_dir,
            self.directories.repo_copy_dir
            )

    def wants_to_run_cleanup(self):
        return True

    def clearDirectoryAsRoot(self, *args):
        if Docker:
            image = Docker.DockerImage("ubuntu:16.04")
            image.run(
                "rm -rf " + " ".join(["%s/*" % p for p in args]), 
                volumes={a:a for a in args}, 
                options="--rm"
                )
        else:
            for a in args:
                try:
                    self.ensureDirectoryExists(a)
                    shutil.rmtree(a)
                    self.ensureDirectoryExists(a)
                except:
                    logging.error("Failure clearing directory %s:\n%s", a, traceback.format_exc())

    def mapInternalToExternalPath(self, path, usingDocker):
        """Given a path within docker, return the path in the host. Returns none if we can't
        find it (because it was part of the docker container's file system)."""

        if not usingDocker:
            return path

        for k,v in self.volumesToExpose().items():
            if path.startswith(v + "/"):
                return k + "/" + path[len(v)+1:]
            elif path == v:
                return k

        return None

    def volumesToExpose(self):
        return {
            self.directories.scratch_dir: "/test_looper/scratch",
            self.directories.test_inputs_dir: "/test_looper/test_inputs",
            self.directories.repo_copy_dir: "/test_looper/src",
            self.directories.test_output_dir: "/test_looper/output",
            self.directories.build_output_dir: "/test_looper/build_output",
            self.directories.ccache_dir: "/test_looper/ccache",
            self.directories.command_dir: "/test_looper/command"
            }

    def _run_deployment(self, env, workerCallback, docker_image, extra_commands, working_directory, extraPorts=None):
        build_log = StringIO.StringIO()

        self.dumpPreambleLog(build_log, env, docker_image, "", working_directory)

        workerCallback.terminalOutput(build_log.getvalue().replace("\n","\r\n"))

        if sys.platform == "win32":
            self._windows_prerun_command()

            assert docker_image is NAKED_MACHINE

            env_to_pass = dict(os.environ)
            env_to_pass.update(env)

            for key in PASSTHROUGH_KEYS:
                if os.getenv(key):
                    env_to_pass[key] = os.getenv(key)

            command_path = os.path.join(self.directories.command_dir,"command.ps1")
            with open(command_path,"w") as cmd_file:
                print >> cmd_file, "cd '" + working_directory + "'"
                print >> cmd_file, "echo 'Welcome to TestLooper on Windows. Here is the current environment:'"
                print >> cmd_file, "gci env:* | sort-object name"
                print >> cmd_file, "echo '********************************'"
                print >> cmd_file, "echo 'HERE ARE AVAILABLE SERVICES:'"
                print >> cmd_file, "Get-Service | Format-Table -Property Name, Status, StartType, DisplayName"
                print >> cmd_file, "echo '********************************'"
                print >> cmd_file, extra_commands

            if workerCallback.localTerminal:
                try:
                    running_subprocess = subprocess.Popen(
                        ["powershell.exe", "-ExecutionPolicy", "Bypass", command_path, "-NoExit"],
                        shell=True,
                        env=env_to_pass,
                        creationflags=0x00000200
                        )
                    running_subprocess.wait()
                except:
                    print "EXCEPTION"
                print "Exiting subshell."
            else:
                invoker_path = os.path.join(self.directories.command_dir,"command_invoker.ps1")

                with open(invoker_path,"w") as cmd_file:
                    print >> cmd_file, "powershell.exe " + command_path
                    print >> cmd_file, "powershell.exe"

                running_subprocess = subprocess.Popen(
                    ["powershell.exe", "-ExecutionPolicy", "Bypass", invoker_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    env=env_to_pass,
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                    )

                logging.info("Powershell process has pid %s", running_subprocess.pid)
                
                time.sleep(.5)

                readthreadStop = threading.Event()
                def readloop(file):
                    try:
                        while not readthreadStop.is_set():
                            data = os.read(file.fileno(), 4096)
                            if not data:
                                #do a little throttling
                                time.sleep(0.01)
                            else:
                                workerCallback.terminalOutput(data.replace("\n","\n\r"))
                    except:
                        logging.error("Read loop failed:\n%s", traceback.format_exc())

                readthreads = [threading.Thread(target=readloop, args=(x,)) for x in [running_subprocess.stdout, running_subprocess.stderr]]
                for t in readthreads:
                    t.daemon=True
                    t.start()

                try:
                    writeFailed = [False]
                    def write(msg):
                        if not msg:
                            return
                        try:
                            if not writeFailed[0]:
                                if isinstance(msg, str):
                                    running_subprocess.stdin.write(msg)
                                elif msg.matches.KeyboardInput:
                                    running_subprocess.stdin.write(msg.bytes)
                        except:
                            writeFailed[0] = True 
                            logging.error("Failed to write to stdin: %s", traceback.format_exc())

                    workerCallback.subscribeToTerminalInput(write)

                    ret_code = None
                    while ret_code is None:
                        try:
                            ret_code = running_subprocess.poll()
                            time.sleep(HEARTBEAT_INTERVAL)
                        except requests.exceptions.ReadTimeout:
                            pass
                        except requests.exceptions.ConnectionError:
                            pass

                        workerCallback.heartbeat()
                finally:
                    try:
                        if ret_code is not None:
                            running_subprocess.terminate()
                    except:
                        logging.info("Failed to terminate subprocess: %s", traceback.format_exc())
                    readthreadStop.set()
        else:
            with open(os.path.join(self.directories.command_dir, "cmd.sh"), "w") as f:
                print >> f, extra_commands

            with open(os.path.join(self.directories.command_dir, "cmd_invoker.sh"), "w") as f:
                print >> f, "hostname testlooperworker"
                print >> f, "bash /test_looper/command/cmd.sh"
                print >> f, "export PS1='${debian_chroot:+($debian_chroot)}\\[\\033[01;32m\\]\\u@\\h\\[\\033[00m\\]:\\[\\033[01;34m\\]\\w\\[\\033[00m\\]\\$ '"
                print >> f, "bash --noprofile --norc"

            assert docker_image is not None

            env = dict(env)
            env["TERM"] = "xterm-256color"

            for key in PASSTHROUGH_KEYS:
                if os.getenv(key):
                    env[key] = os.getenv(key)

            
            with DockerWatcher.DockerWatcher(self.name_prefix + str(uuid.uuid4()) + "_") as watcher:
                if isinstance(workerCallback, DummyWorkerCallbacks) and workerCallback.localTerminal:
                    kwargs = {}
                    if extraPorts:
                        logging.info("Exposing extra ports: %s", extraPorts)
                        kwargs["ports"] = extraPorts

                    container = watcher.run(
                        docker_image,
                        ["/bin/bash", "/test_looper/command/cmd_invoker.sh"],
                        volumes=self.volumesToExpose(),
                        privileged=True,
                        shm_size="1G",
                        environment=env,
                        working_dir=working_directory,
                        tty=True,
                        stdin_open=True,
                        start=False,
                        **kwargs
                        )
                    import dockerpty

                    client = docker.from_env()
                    client.__dict__["start"] = lambda c, *args, **kwds: client.api.start(c.id, *args, **kwds)
                    client.__dict__["inspect_container"] = lambda c: client.api.inspect_container(c.id)
                    client.__dict__["attach_socket"] = lambda c,*args,**kwds: client.api.attach_socket(c.id, *args, **kwds)
                    client.__dict__["resize"] = lambda c,*args,**kwds: client.api.resize(c.id, *args, **kwds)
                    dockerpty.start(client, container)
                else:
                    container = watcher.run(
                        docker_image,
                        ["/bin/bash", "/test_looper/command/cmd_invoker.sh"],
                        volumes=self.volumesToExpose(),
                        privileged=True,
                        shm_size="1G",
                        environment=env,
                        working_dir=working_directory,
                        tty=True,
                        stdin_open=True
                        )

                    #these are standard socket objects connected to the container's TTY input/output
                    stdin = docker.from_env().api.attach_socket(container.id, params={'stdin':1,'stream':1,'logs':None})
                    stdout = docker.from_env().api.attach_socket(container.id, params={'stdout':1,'stream':1,'logs':None})

                    readthreadStop = threading.Event()
                    def readloop():
                        while not readthreadStop.is_set():
                            data = stdout.recv(4096)
                            if not data:
                                logging.info("Socket stdout connection to %s terminated", container.id)
                                return
                            workerCallback.terminalOutput(data)

                    readthread = threading.Thread(target=readloop)
                    readthread.start()

                    stdin.sendall("\n")

                    writeFailed = [False]
                    def write(msg):
                        if not msg:
                            return
                        try:
                            if not writeFailed[0]:
                                if isinstance(msg, str):
                                    stdin.sendall(msg)
                                elif msg.matches.KeyboardInput:
                                    stdin.sendall(msg.bytes)
                                elif msg.matches.Resize:
                                    logging.info("Terminal resizing to %s cols and %s rows", msg.cols, msg.rows)
                                    container.resize(msg.rows, msg.cols)
                        except:
                            writeFailed[0] = True 
                            logging.error("Failed to write to stdin: %s", traceback.format_exc())

                    workerCallback.subscribeToTerminalInput(write)
                    
                    try:
                        t0 = time.time()
                        ret_code = None
                        extra_message = None
                        while ret_code is None:
                            try:
                                ret_code = container.wait(timeout=HEARTBEAT_INTERVAL)
                            except requests.exceptions.ReadTimeout:
                                pass
                            except requests.exceptions.ConnectionError:
                                pass

                            workerCallback.heartbeat()
                    finally:
                        try:
                            container.remove(force=True)
                        except:
                            pass
                        readthreadStop.set()
                        readthread.join()
                        
    def dumpPreambleLog(self, build_log, env, docker_image, command, working_directory):
        print >> build_log, "********************************************"

        print >> build_log, "TestLooper Environment Variables:"
        for e in sorted(env):
            print >> build_log, "\t%s=%s" % (e, env[e])
        print >> build_log

        if docker_image is not NAKED_MACHINE:
            print >> build_log, "DockerImage is ", docker_image.image
        build_log.flush()

        print >> build_log, "Working Directory: " + working_directory
        build_log.flush()

        if command:
            print >> build_log, "TestLooper Running command:"
            print >> build_log, command
            build_log.flush()

        print >> build_log, "********************************************"
        print >> build_log
        build_log.flush()


    def _run_test_command(self, command, timeout, env, log_function, docker_image, working_directory, dumpPreambleLog=True):
        if sys.platform == "win32":
            return self._run_test_command_windows(command, timeout, env, log_function, docker_image, working_directory, dumpPreambleLog)
        else:
            return self._run_test_command_linux(command, timeout, env, log_function, docker_image, working_directory, dumpPreambleLog)

    def _windows_prerun_command(self):
        pass

    def _run_test_command_windows(self, command, timeout, env, log_function, docker_image, working_directory, dumpPreambleLog):
        self._windows_prerun_command()

        assert docker_image is NAKED_MACHINE

        env_to_pass = dict(os.environ)

        for k,v in sorted(env.iteritems()):
            env_to_pass[k.upper()] = v

        for key in PASSTHROUGH_KEYS:
            if os.getenv(key):
                env_to_pass[key] = os.getenv(key)

        t0 = time.time()

        #generate a vars file to override the current environment if we want to 'pop into' this 
        #session later.
        with open(os.path.join(self.directories.command_dir,"vars.bat"), "w") as f:
            print >> f, "@ECHO OFF"
            print >> f, "REM AUTOGENERATED BATCH VARIABLES"
            
            def escape(v):
                v = v.replace("%", "%%")
                for char in "^&/<>|":
                    v = v.replace(char, "^" + char)
                return v

            for k,v in env.iteritems():
                print >> f, "SET %s=%s" % (k, escape(v))
            print >> f, "@ECHO ON"

        #generate a vars file to override the current environment if we want to 'pop into' this 
        #session later.
        with open(os.path.join(self.directories.command_dir,"vars.ps1"), "w") as f:
            def escape(v):
                for char in "`$'\"":
                    v = v.replace(char, "`" + char)
                return v

            for k,v in env.iteritems():
                print >> f, '$env:%s="%s"' % (k, escape(v))


        command_path = os.path.join(self.directories.command_dir,"command.ps1")
        with open(command_path,"w") as cmd_file:
            print >> cmd_file, "cd '" + working_directory + "'"
            if dumpPreambleLog:
                print >> cmd_file, "echo 'Welcome to TestLooper on Windows. Here is the current environment:'"
                print >> cmd_file, "gci env:* | sort-object name"
                print >> cmd_file, "echo '********************************'"
                print >> cmd_file, "echo 'HERE ARE AVAILABLE SERVICES:'"
                print >> cmd_file, "Get-Service | Format-Table -Property Name, Status, StartType, DisplayName"
                print >> cmd_file, "echo '********************************'"
                print >> cmd_file, "echo 'REBOOT SCRIPT ******************'"
                print >> cmd_file, """cat "C:\ProgramData\TestLooper\PreWorkerStartup.ps1" """
                print >> cmd_file, "echo 'REBOOT SCRIPT OUTPUT ***********'"
                print >> cmd_file, """cat "C:\ProgramData\TestLooper\PreWorkerStartup.log" """
                print >> cmd_file, "echo '********************************'"
                
            print >> cmd_file, command
            print >> cmd_file, "exit $lastexitcode"

        running_subprocess = subprocess.Popen(
            ["powershell.exe", "-ExecutionPolicy", "Bypass", command_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            env=env_to_pass,
            creationflags=subprocess.CREATE_NEW_CONSOLE
            )

        logging.info("Powershell process has pid %s", running_subprocess.pid)
        time.sleep(.5)

        readthreadStop = threading.Event()
        def readloop(file):
            try:
                while not readthreadStop.is_set():
                    data = os.read(file.fileno(), 4096)
                    if not data:
                        #do a little throttling
                        time.sleep(0.01)
                    else:
                        log_function(data)
            except:
                logging.error("Read loop failed:\n%s", traceback.format_exc())

        readthreads = [threading.Thread(target=readloop, args=(x,)) for x in [running_subprocess.stdout, running_subprocess.stderr]]
        for t in readthreads:
            t.daemon=True
            t.start()

        try:
            ret_code = None
            while ret_code is None:
                try:
                    ret_code = running_subprocess.poll()
                    time.sleep(HEARTBEAT_INTERVAL)
                except requests.exceptions.ReadTimeout:
                    pass
                except requests.exceptions.ConnectionError:
                    pass

                log_function("")

                if time.time() - t0 > timeout:
                    log_function("\n\n" + time.asctime() + " TestLooper> Process timed out (%s seconds).\n" % timeout)
                    running_subprocess.terminate()
                    return False
        finally:
            try:
                if ret_code is not None:
                    running_subprocess.terminate()
            except:
                logging.info("Failed to terminate subprocess: %s", traceback.format_exc())

            readthreadStop.set()
        
        log_function("\n\n" + time.asctime() + " TestLooper> Process exited with code %s\n" % ret_code)

        return ret_code == 0

    def _run_test_command_linux(self, command, timeout, env, log_function, docker_image, working_directory, dumpPreambleLog):
        tail_proc = None
        
        try:
            log_filename = os.path.join(self.directories.command_dir, "log.txt")

            with open(log_filename, 'w') as build_log:
                tail_proc = SubprocessRunner.SubprocessRunner(["tail","-f",log_filename,"-n","+0"], log_function, log_function, enablePartialLineOutput=True)
                tail_proc.start()

                if dumpPreambleLog:
                    self.dumpPreambleLog(build_log, env, docker_image, command, working_directory)
                else:
                    print >> build_log, "TestLooper Running command"
                    print >> build_log, command
                    print >> build_log, "********************************************"
                    print >> build_log
                    build_log.flush()

            logging.info("Running command: '%s'. Log: %s. Docker Image: %s", 
                command, 
                log_filename,
                docker_image.image if docker_image is not None else "<none>"
                )

            with open(os.path.join(self.directories.command_dir, "cmd.sh"), "w") as f:
                print >> f, command

            with open(os.path.join(self.directories.command_dir, "cmd_invoker.sh"), "w") as f:
                print >> f, "hostname testlooperworker"
                print >> f, "bash /test_looper/command/cmd.sh >> /test_looper/command/log.txt 2>&1"

            assert docker_image is not None

            env = dict(env)
            for key in PASSTHROUGH_KEYS:
                if os.getenv(key):
                    env[key] = os.getenv(key)

            with DockerWatcher.DockerWatcher(self.name_prefix + str(uuid.uuid4()) + "_") as watcher:
                container = watcher.run(
                    docker_image,
                    ["/bin/bash", "/test_looper/command/cmd_invoker.sh"],
                    volumes=self.volumesToExpose(),
                    privileged=True,
                    shm_size="1G",
                    environment=env,
                    working_dir=working_directory
                    )

                t0 = time.time()
                ret_code = None
                extra_message = None
                while ret_code is None:
                    try:
                        ret_code = container.wait(timeout=HEARTBEAT_INTERVAL)
                    except requests.exceptions.ReadTimeout:
                        pass
                    except requests.exceptions.ConnectionError:
                        pass

                    log_function("")
                    if time.time() - t0 > timeout:
                        ret_code = 1
                        container.stop()
                        extra_message = "Test timed out, so we're stopping the test."


                with open(log_filename, 'a') as build_log:
                    print >> build_log, container.logs()
                    print >> build_log
                    if extra_message:
                        print >> build_log, extra_message
                    print >> build_log, "Process exited with code ", ret_code
                    build_log.flush()
                    
            return ret_code == 0
        finally:
            if tail_proc is not None:
                tail_proc.stop()

    def resetToCommitInDir(self, repoName, commitHash, pathWithinRepo, targetDir):
        git_repo = self.getRepoCacheByName(repoName)

        if not git_repo.isInitialized():
            git_repo.cloneFrom(self.source_control.getRepo(repoName).cloneUrl())

        git_repo.resetToCommitInDirectory(commitHash, targetDir)
        os.unlink(os.path.join(targetDir, ".git"))

        if pathWithinRepo:
            #we want to checkout path A/B/C (or something like that), which we assume
            #is a directory. If not, the checkout will be empty except for the commit
            subdir = os.path.join(targetDir, pathWithinRepo)

            #if it exists, move the subdirectory to a safe location
            if os.path.isdir(subdir):
                guid = str(uuid.uuid4()).replace("-","")
                movedPath = os.path.join(self.directories.scratch_dir, guid)

                shutil.move(subdir, movedPath)
            else:
                movedPath = None

            shutil.rmtree(targetDir)

            if movedPath:
                shutil.move(movedPath, targetDir)
            else:
                os.makedirs(targetDir)
                
        with open(os.path.join(targetDir, ".git_commit"), "w") as f:
            f.write(git_repo.standardCommitMessageFor(commitHash))

    @staticmethod
    def ensureDirectoryExists(path):
        if os.path.exists(path):
            return
        try:
            os.makedirs(path)
        except os.error as e:
            if e.errno != errno.EEXIST:
                raise

    def purge_build_cache(self, cacheSize=None):
        self.ensureDirectoryExists(self.directories.build_cache_dir)
        
        while self._is_build_cache_full(cacheSize if cacheSize is not None else self.max_build_cache_depth):
            self._remove_oldest_cached_build()

    def _is_build_cache_full(self, cacheSize):
        cache_count = len(os.listdir(self.directories.build_cache_dir))

        logging.info("Checking the build cache: there are %s items in it", cache_count)

        return cache_count > cacheSize

    def _remove_oldest_cached_build(self):
        def full_path(p):
            return os.path.join(self.directories.build_cache_dir, p)
        cached_builds = sorted([(os.path.getctime(full_path(p)), full_path(p))
                                for p in os.listdir(self.directories.build_cache_dir)])
        os.remove(cached_builds[0][1])

    def getDockerImage(self, testEnvironment, log_function):
        assert testEnvironment.matches.Environment
        assert testEnvironment.platform.matches.linux
        assert testEnvironment.image.matches.Dockerfile or testEnvironment.image.matches.DockerfileInline

        try:
            if testEnvironment.image.matches.Dockerfile:
                assert False, "This should have been resolved to dockerfile contents already."
            else:
                return Docker.DockerImage.from_dockerfile_as_string(
                    self.docker_image_repo, 
                    testEnvironment.image.dockerfile_contents, 
                    create_missing=True, 
                    env_keys_to_passthrough=PASSTHROUGH_KEYS,
                    logger=withTime(log_function)
                    )
        except Exception as e:
            log_function(time.asctime() + " TestLooper> Failed to build docker image:\n" + str(e))

        return None

    def runTest(self, testId, workerCallback, testDefinition, isDeploy, extraPorts=None, command_override=None):
        """Run a test (given by name) on a given commit and return a TestResultOnMachine"""
        self.cleanup()

        testName = testDefinition.name

        t0 = time.time()

        log_messages = []
        def log_function(msg=""):
            if isDeploy:
                if msg is not None:
                    workerCallback.terminalOutput(msg.replace("\n", "\r\n"))
            else:
                workerCallback.heartbeat(msg)

            if msg is not None:
                log_messages.append(msg)

        def executeTest():
            try:
                artifactNames = [artifact.name for stage in testDefinition.stages for artifact in stage.artifacts]
                fullArtifactNames = [testName + ("/" if name else "") + name for name in artifactNames]

                allExist = all([self.artifactStorage.build_exists(testDefinition.hash, self.artifactKeyForBuild(name))
                    for name in fullArtifactNames])

                if not isDeploy and testDefinition.matches.Build and allExist:
                    log_function("Build already exists\n")
                    for a in artifactNames:
                        workerCallback.recordArtifactUploaded(a)
                    return True, {}
                
                return self._run_task(testId, testDefinition, log_function, workerCallback, isDeploy, extraPorts, command_override)
            except KeyboardInterrupt:
                log_function("\nInterrupted by Ctrl-C\n")
                return False, {}
            except:
                print "*******************"
                print traceback.format_exc()
                print "*******************"
                error_message = "Test failed because of exception: %s" % traceback.format_exc()
                logging.error(error_message)
                log_function(error_message)
                return False, {}


        success, individualTestSuccesses = executeTest()

        if isDeploy:
            return False, {}

        try:
            with self.callHeartbeatInBackground(log_function, "Uploading logfiles."):
                path = os.path.join(self.directories.scratch_dir, "test_result.json")
                with open(path, "w") as f:
                    f.write(
                        json.dumps(
                            {"success": success,
                             "individualTests": individualTestSuccesses,
                             "start_timestamp": t0,
                             "end_timestamp": time.time()
                            })
                        )
                        
                self.artifactStorage.uploadSingleTestArtifact(testDefinition.hash, testId, "test_result.json", path)

                path = os.path.join(self.directories.scratch_dir, "test_looper_log.txt")
                with open(path, "w") as f:
                    f.write("".join(log_messages))

                self.artifactStorage.uploadSingleTestArtifact(testDefinition.hash, testId, "test_looper_log.txt", path)

        except:
            log_function("ERROR: Failed to upload the testlooper logfile to artifactStorage:\n\n%s" % traceback.format_exc())
        finally:
            withTime(log_function)("Finished uploading artifacts.")

        return success, individualTestSuccesses



    def extract_package(self, package_file, target_dir):
        with tarfile.open(package_file, "r|gz") as tar:
            logging.info("Extracting package %s to %s", package_file, target_dir)
            tar.extractall(target_dir)

    def grabDependency(self, log_function, expose_as, dep, worker_callback):
        target_dir = os.path.join(self.directories.worker_directory, expose_as)

        if dep.matches.Build:
            full_name = dep.name + ("/" if dep.artifact else "") + dep.artifact

            if not self.artifactStorage.build_exists(dep.buildHash, self.artifactKeyForBuild(full_name)):
                return "can't run tests because dependent external build %s doesn't exist" % (dep.buildHash + "/" + full_name)

            path = self._download_build(dep.buildHash, full_name, log_function)
            
            log_function(time.asctime() + " TestLooper> Extracting tarball for %s/%s.\n" % (dep.buildHash, full_name))

            self.ensureDirectoryExists(target_dir)
            self.extract_package(path, target_dir)

            return None

        if dep.matches.Source:
            #keep the source tarballs separate by os-root, since windows line endings
            #play havoc with linux builds!
            source_platform_name = "source-linux" if sys.platform != "win32" else "source-win"

            if dep.path:
                source_platform_name = os.path.join(source_platform_name, dep.path)

            sourceArtifactName = self.artifactKeyForBuild(source_platform_name)

            tarball_name = self._buildCachePathFor(dep.commitHash, source_platform_name)

            log_function(time.asctime() + " TestLooper> Target tarball for %s/%s source is %s\n" 
                        % (dep.repo, dep.commitHash, tarball_name))

            if not self.artifactStorage.build_exists(dep.commitHash, sourceArtifactName):
                log_function(time.asctime() + " TestLooper> Building source cache for %s/%s at %s\n" 
                        % (dep.repo, dep.commitHash, target_dir))

                if os.path.exists(target_dir):
                    shutil.rmtree(target_dir)

                with worker_callback.scopedReadLockAroundGitRepo():
                    self.resetToCommitInDir(dep.repo, dep.commitHash, dep.path, target_dir)

                with tarfile.open(tarball_name, "w:gz", compresslevel=1) as tf:
                    tf.add(target_dir, ".")

                log_function(time.asctime() + " TestLooper> Resulting tarball at %s is %.2f MB.\n" %(tarball_name, os.stat(tarball_name).st_size / 1024.0**2))

                try:
                    log_function(
                        time.asctime() + " TestLooper> Uploading %s to %s/%s/%s\n" % 
                            (tarball_name, dep.repo, dep.commitHash, sourceArtifactName)
                        )
                    self.artifactStorage.upload_build(dep.commitHash, sourceArtifactName, tarball_name)
                except:
                    log_function(time.asctime() + " TestLooper> Failed to upload package '%s':\n%s" % (
                          tarball_name,
                          traceback.format_exc()
                          ))
            else:
                if not os.path.exists(tarball_name):
                    log_function(time.asctime() + " TestLooper> Downloading source cache for %s/%s.\n" % (dep.repo, dep.commitHash))
                
                    self.artifactStorage.download_build(dep.commitHash, sourceArtifactName, tarball_name)

                log_function(time.asctime() + " TestLooper> Extracting source cache for %s/%s.\n" % (dep.repo, dep.commitHash))

                self.extract_package(tarball_name, target_dir)

            return None

        return "Unknown dependency type: %s" % dep

    def getEnvironmentAndDependencies(self, testId, test_definition, log_function, worker_callback):
        environment = test_definition.environment
        
        env_overrides = self.environment_variables(testId, environment, test_definition)

        #update the test definition to resolve dependencies given our base environment overrides
        test_definition = TestDefinition.apply_variable_substitution_to_test(test_definition, env_overrides)

        all_dependencies = {}
        all_dependencies.update(environment.dependencies)
        all_dependencies.update(test_definition.dependencies)

        image = [None]
        image_exception = [None]

        lock = threading.Lock()

        def heartbeatWithLock(msg=None):
            with lock:
                log_function(msg)

        with self.callHeartbeatInBackground(
                heartbeatWithLock, 
                "Pulling dependencies:\n%s" % "\n".join(["\t%s -> %s" % (k,v) for k,v in sorted(all_dependencies.iteritems())])
                ):

            results = {}

            def pullImage():
                if environment.image.matches.AMI:
                    image[0] = NAKED_MACHINE
                else:
                    try:
                        image[0] = self.getDockerImage(environment, heartbeatWithLock)
                    except:
                        image_exception[0] = traceback.format_exc()


            def callFun(expose_as, dep):
                for tries in xrange(3):
                    try:
                        results[expose_as] = self.grabDependency(heartbeatWithLock, expose_as, dep, worker_callback)
                        heartbeatWithLock(time.asctime() + " TestLooper> Done pulling %s.\n" % dep)
                        return
                    except Exception as e:
                        if tries < 2:
                            heartbeatWithLock(time.asctime() + " TestLooper> Failed to pull %s because %s, but retrying.\n" % (dep, str(e)))

                        results[expose_as] = traceback.format_exc()

            waiting_threads = []

            waiting_threads.append(threading.Thread(target=pullImage))

            for expose_as_and_dep in all_dependencies.iteritems():
                waiting_threads.append(threading.Thread(target=callFun, args=expose_as_and_dep))

            running_threads = []

            simultaneous = self.hardwareConfig.cores

            while running_threads + waiting_threads:
                running_threads = [x for x in running_threads if x.isAlive()]
                while len(running_threads) < simultaneous and waiting_threads:
                    t = waiting_threads.pop(0)
                    t.start()
                    running_threads.append(t)
                time.sleep(1.0)

            for e in all_dependencies:
                if results[e] is not None:
                    raise Exception("Failed to download dependency %s: %s" % (all_dependencies[e], results[e]))

            if image_exception[0]:
                raise Exception(image_exception[0])
        
        return environment, all_dependencies, test_definition, image[0]

    def _run_task(self, testId, test_definition, log_function, workerCallback, isDeploy, extraPorts, command_override):
        try:
            environment, all_dependencies, test_definition, image = \
                self.getEnvironmentAndDependencies(testId, test_definition, log_function, workerCallback)
        except Exception as e:
            logging.error(traceback.format_exc())
            log_function("\n\nTest failed because of exception:\n" + traceback.format_exc() + "\n")
            return False, {}

        if test_definition.matches.Build:
            stages = test_definition.stages
        elif test_definition.matches.Test:
            stages = test_definition.stages
        elif test_definition.matches.Deployment:
            stages = []
        else:
            assert False, test_definition

        if command_override is not None:
            stages = [
                TestDefinition.Stage.Stage(
                    command=command_override,
                    cleanup="",
                    artifacts=[],
                    order=0.0
                    )
                ]

        is_success = True

        if image is None:
            is_success = False
            if isDeploy:
                log_function("Couldn't find docker image...")
                return False, {}
        else:
            logging.info("Machine %s is starting run for %s.",
                         self.machineId,
                         test_definition.hash
                         )

            if image is NAKED_MACHINE:
                working_directory = self.directories.test_inputs_dir
            else:
                working_directory = "/test_looper/test_inputs"

            if isDeploy:
                extra_commands = "\n\n".join([s.command for s in stages])

                self._run_deployment(test_definition.variables, workerCallback, image, extra_commands, working_directory, extraPorts=extraPorts)
                return False, {}
            else:
                for stage in stages:
                    is_success = self._runStage(testId, stage, image, test_definition, working_directory, log_function, workerCallback)
                    
                    if is_success == EARLY_STOP:
                        is_success = True
                        break

                    if not is_success:
                        break

        individualTestSuccesses = self.finalTestArtifactUpload(image, testId, test_definition, log_function)

        return is_success, individualTestSuccesses

    def _runStage(self, testId, stage, image, test_definition, working_directory, log_function, workerCallback):
        withTime(log_function)("Starting Test Run")

        is_success = self._run_test_command(
            stage.command,
            test_definition.timeout or 60 * 60, #1 hour if unspecified
            test_definition.variables,
            log_function,
            image,
            working_directory, 
            dumpPreambleLog=True
            )

        #run the cleanup_command if necessary
        if self.wants_to_run_cleanup() and stage.cleanup.strip():
            if not self._run_test_command(
                    stage.cleanup,
                    test_definition.timeout or 60 * 60, #1 hour if unspecified
                    test_definition.variables,
                    log_function,
                    image,
                    working_directory, 
                    dumpPreambleLog=False
                    ):
                is_success = False

        if test_definition.matches.Build:
            if is_success:
                for artifact in stage.artifacts:
                    with self.callHeartbeatInBackground(log_function, "Uploading build artifact for %s/%s." % (test_definition.name, artifact.name)):
                        if not self._upload_artifact(test_definition.hash, testId, test_definition.name, artifact, False, log_function, image):
                            is_success = False
                        else:
                            if workerCallback.recordArtifactUploaded(artifact.name):
                                is_success = EARLY_STOP
        else:
            for artifact in stage.artifacts:
                with self.callHeartbeatInBackground(log_function, "Uploading build artifact for %s/%s." % (test_definition.name, artifact.name)):
                    if not self._upload_artifact(test_definition.hash, testId, test_definition.name, artifact, True, log_function, image):
                        is_success = False
                    else:
                        workerCallback.recordArtifactUploaded(artifact.name)

        return is_success


    def finalTestArtifactUpload(self, image, testId, test_definition, log_function):
        individualTestSuccesses = {}

        with self.callHeartbeatInBackground(log_function, "Uploading test artifacts."):
            testSummaryJsonPath = os.path.join(self.directories.test_output_dir, "testSummary.json")

            if os.path.exists(testSummaryJsonPath):
                try:
                    contents = open(testSummaryJsonPath,"r").read()
                    individualTestSuccesses = json.loads(contents)

                    if not isinstance(individualTestSuccesses, dict):
                        raise Exception("testSummary.json should be a dict from str to bool")

                    pathsToUpload = {}

                    def processTestSuccess(keyname, entry):
                        if isinstance(entry, dict) and 'logs' not in entry:
                            entry['logs'] = []
                        
                        if isinstance(entry, bool):
                            return (entry, False)
                        elif (isinstance(entry, dict) and
                                'success' in entry and
                                isinstance(entry['success'], bool) and
                                'logs' in entry and
                                not [x for x in entry['logs'] if not isinstance(x, (str, unicode))]
                                ):
                            
                            for path in entry['logs']:
                                pathVisibleToWorker = self.mapInternalToExternalPath(path, image is not NAKED_MACHINE)

                                if not pathVisibleToWorker:
                                    withTime(log_function)("Test output path %s not visible outside of the docker container!" % path)
                                    return {'success': False, 'logs': False}

                                pathsToUpload[keyname] = pathsToUpload.get(keyname,()) + (pathVisibleToWorker,)

                            return (entry['success'], True)
                        else:
                            withTime(log_function)("testSummary.json entries should be bools or {'success': Bool, 'logs': ['str']}, not %s" % entry)
                            return (False, False)

                    individualTestSuccesses = {str(k): processTestSuccess(k, v) for k,v in individualTestSuccesses.iteritems()}

                    if pathsToUpload:
                        self.artifactStorage.uploadIndividualTestArtifacts(test_definition.hash, testId, pathsToUpload, withTime(log_function))

                except Exception as e:
                    individualTestSuccesses = {}
                    log_function("Failed to pull in testSummary.json: " + str(e))
                    logging.error("Error processing testSummary.json:\n%s", traceback.format_exc())

        return individualTestSuccesses

    def artifactKeyForBuild(self, testName):
        return self.artifactStorage.sanitizeName(testName) + ".tar.gz"

    def mapArtifactDirectoryToAbspath(self, dir, isTest, isNaked):
        if os.path.isabs(dir):
            return dir
        if isNaked:
            #this is on windows
            if isTest:
                return self.directories.test_output_dir
            else:
                return self.directories.build_output_dir
        else:
            if isTest:
                return "/test_looper/output"
            else:
                return "/test_looper/build_output"

    def _upload_artifact(self, testDefHash, testId, testName, artifact, isTestArtifact, log_function, image):
        intendedDirectory = self.mapArtifactDirectoryToAbspath(artifact.directory, isTestArtifact, image is NAKED_MACHINE)

        artifactDirectory = self.mapInternalToExternalPath(intendedDirectory, image is not NAKED_MACHINE)

        if not artifactDirectory:
            withTime(log_function)("Error: path %s isn't visible outside of the docker container", intendedDirectory)
            return False

        #upload all the data in our directory
        full_name = testName + ("/" + artifact.name if artifact.name else "")
        
        tarball_name = self._buildCachePathFor(testDefHash, full_name)

        if os.path.exists(tarball_name):
            logging.warn("A build for %s/%s already exists at %s", testDefHash, full_name, tarball_name)
            os.remove(tarball_name)

        withTime(log_function)("Tarballing %s into %s", intendedDirectory, tarball_name)

        with tarfile.open(tarball_name, "w:gz", compresslevel=1) as tf:
            def filter(tarinfo):
                name = tarinfo.name

                if name.startswith("./"):
                    name = name[2:]

                if not tarinfo.isfile():
                    return tarinfo

                anyIncluding = True
                if artifact.include_patterns:
                    anyIncluding = False
                    for glob in artifact.include_patterns:
                        if fnmatch.fnmatchcase(name, glob):
                            anyIncluding = True
                            break

                if not anyIncluding:
                    return None

                for glob in artifact.exclude_patterns:
                    if fnmatch.fnmatchcase(name, glob):
                        return None

                return tarinfo

            if os.path.exists(artifactDirectory):
                tf.add(artifactDirectory, ".", filter=filter)
            else:
                withTime(log_function)("Warning: directory %s doesnt exist", intendedDirectory)

        withTime(log_function)("Resulting tarball at %s is %.2f MB", tarball_name, os.stat(tarball_name).st_size / 1024.0**2)

        try:
            withTime(log_function)("Uploading %s", tarball_name)

            if isTestArtifact:
                self.artifactStorage.uploadSingleTestArtifact(testDefHash, testId, self.artifactKeyForBuild(full_name), tarball_name)
            else:
                self.artifactStorage.upload_build(testDefHash, self.artifactKeyForBuild(full_name), tarball_name)

            withTime(log_function)("Done uploading %s", tarball_name)

            return True
        except:
            withTime(log_function)("ERROR: Failed to upload package '%s':\n%s",
                          tarball_name,
                          traceback.format_exc()
                          )
            return False

    def _buildCachePathFor(self, testDefHash, testName):
        return os.path.join(
            self.directories.build_cache_dir,
            (testDefHash + "_" + self.artifactKeyForBuild(testName))
            )

    def _download_build(self, buildHash, testName, log_function):
        path = self._buildCachePathFor(buildHash, testName)
        
        if not os.path.exists(path):
            log_function(time.asctime() + " TestLooper> " + "Downloading build for %s test %s\n" % (buildHash, testName))
            log_function(time.asctime() + " TestLooper> " + "    to %s.\n" % (path))

            self.artifactStorage.download_build(buildHash, self.artifactKeyForBuild(testName), path)

        return path

    def path_to_git_for_windows_installation(self):
        for path in os.getenv("PATH").split(";"):
            if os.path.exists(os.path.join(path, "git.exe")):
                return os.path.dirname(path)

    def environment_variables(self, testId, environment, test_definition):
        res = {}
        res.update({
            'TEST_CORES_AVAILABLE': str(self.hardwareConfig.cores),
            'TEST_RAM_GB_AVAILABLE': str(self.hardwareConfig.ram_gb),
            'PYTHONUNBUFFERED': "TRUE",
            'HOSTNAME': "testlooperworker"
            })

        if sys.platform == "win32":
            git_path = self.path_to_git_for_windows_installation()

            if git_path:
                res["GIT_PERL_BIN"] = os.path.join(git_path, "usr", "bin", "perl.exe")
                res["GIT_BIN"] = os.path.join(git_path, "cmd", "git.exe")

        has_implicit_src_dep = "src" in test_definition.dependencies
        if has_implicit_src_dep:
            if environment.image.matches.AMI:
                res["TEST_SRC_DIR"] = self.directories.repo_copy_dir
            else:
                res["TEST_SRC_DIR"] = "/test_looper/src"

        if environment.image.matches.AMI:
            res.update({
                'TEST_INPUTS': self.directories.test_inputs_dir,
                'TEST_SCRATCH_DIR': self.directories.scratch_dir,
                'TEST_OUTPUT_DIR': self.directories.test_output_dir,
                'TEST_BUILD_OUTPUT_DIR': self.directories.build_output_dir,
                'TEST_CCACHE_DIR': self.directories.ccache_dir
                })
        else:
            res.update({
                'TEST_INPUTS': "/test_looper/test_inputs",
                'TEST_SCRATCH_DIR': "/test_looper/scratch",
                'TEST_OUTPUT_DIR': "/test_looper/output",
                'TEST_BUILD_OUTPUT_DIR': "/test_looper/build_output",
                'TEST_CCACHE_DIR': "/test_looper/ccache"
                })

        if testId is not None:
            res['TEST_LOOPER_TEST_ID'] = testId

        return res
