import cherrypy
import dateutil.parser
import itertools
import math
import os
import sys
import time
import logging
import tempfile
import threading
import markdown
import urllib
import urlparse
import pytz
import simplejson
import os
import test_looper.core.DirectoryScope as DirectoryScope
import test_looper.core.SubprocessRunner as SubprocessRunner
import test_looper.core.source_control as Github
import test_looper.server.HtmlGeneration as HtmlGeneration
from ws4py.server.cherrypyserver import WebSocketPlugin, WebSocketTool
from ws4py.websocket import WebSocket

import traceback

time.tzset()

def secondsUpToString(up_for):
    if up_for < 60:
        return ("%d seconds" % up_for)
    elif up_for < 60 * 60 * 2:
        return ("%d minutes" % (up_for / 60))
    elif up_for < 24 * 60 * 60 * 2:
        return ("%.1f hours" % (up_for / 60 / 60))
    else:
        return ("%.1f days" % (up_for / 60 / 60 / 24))


def joinLinks(linkList):
    res = ""

    for l in linkList:
        if res:
            res = res + ", "
        res = res + l

    return res

class LogHandler:
    def __init__(self, testManager, testId, websocket):
        self.testManager = testManager
        self.testId = testId
        self.websocket = websocket

        testManager.heartbeatHandler.addListener(testId, self.logMsg)

    def logMsg(self, message):
        try:
            self.websocket.send(message.replace("\n","\n\r"), False)
        except:
            logging.error("error in websocket handler:\n%s", traceback.format_exc())

    def onData(self, message):
        pass

    def onClosed(self):
        pass

class InteractiveEnvironmentHandler:
    def __init__(self, testManager, deploymentId, websocket):
        self.testManager = testManager
        self.deploymentId = deploymentId
        self.websocket = websocket

        testManager.subscribeToDeployment(self.deploymentId, self.onTestOutput)

    def onTestOutput(self, testOutput):
        try:
            if testOutput:
                self.websocket.send(testOutput, False)
        except:
            logging.error("Error in websocket handler: \n%s", traceback.format_exc())

    def onData(self, message):
        self.testManager.writeMessageToDeployment(self.deploymentId, message)

    def onClosed(self):
        self.testManager.unsubscribeFromDeployment(self.deploymentId, self.onTestOutput)



def MakeWebsocketHandler(httpServer):
    def caller(*args, **kwargs):
        everGotAMessage = [False]
        handler = [None]

        class WebsocketHandler(WebSocket):
            def logMsg(self, message):
                try:
                    self.send(message.replace("\n","\n\r"), False)
                except:
                    logging.error("error in websocket handler:\n%s", traceback.format_exc())

            def initialize(self):
                try:
                    try:
                        query = urlparse.parse_qs(urlparse.urlparse(self.environ["REQUEST_URI"]).query)
                    except:
                        self.send("Invalid query string.", False)
                        return

                    if "testId" in query:
                        handler[0] = LogHandler(httpServer.testManager, query["testId"][0], self)
                    elif "deploymentId" in query:
                        handler[0] = InteractiveEnvironmentHandler(
                            httpServer.testManager,
                            query['deploymentId'][0],
                            self 
                            )
                    else:
                        logging.error("Invalid query string: %s", self.environ["REQUEST_URI"])
                        self.send("Invalid query string.", False)
                        return
                except:
                    logging.error("error in websocket handler:\n%s", traceback.format_exc())

            def received_message(self, message):
                try:
                    msg = message.data

                    if not everGotAMessage[0]:
                        everGotAMessage[0] = True
                        self.initialize()
                        msg = msg[1:]

                    if handler[0]:
                        handler[0].onData(msg)
                except:
                    logging.error("error in websocket handler:\n%s", traceback.format_exc())

            def closed(self, *args):
                try:
                    WebSocket.closed(self, *args)

                    if handler[0]:
                        handler[0].onClosed()
                except:
                    logging.error("error in websocket handler:\n%s", traceback.format_exc())


        return WebsocketHandler(*args, **kwargs)
    return caller


class TestLooperHttpServer(object):
    def __init__(self,
                 portConfig,
                 serverConfig,
                 testManager,
                 machine_management,
                 artifactStorage,
                 src_ctrl,
                 event_log
                 ):
        """Initialize the TestLooperHttpServer

        testManager - a TestManager.TestManager object
        httpPortOverride - the port to listen on for http requests
        """
        self.testManager = testManager
        self.machine_management = machine_management
        self.httpPort = portConfig.server_https_port
        self.src_ctrl = src_ctrl
        self.eventLog = event_log
        self.eventLog.addLogMessage("test-looper", "TestLooper initialized")
        self.defaultCoreCount = 4
        self.artifactStorage = artifactStorage
        self.certs = serverConfig.path_to_certs.val if serverConfig.path_to_certs.matches.Value else None
        self.address = ("https" if self.certs else "http") + "://" + portConfig.server_address + ":" + str(portConfig.server_https_port)
        self.websocket_address = ("wss" if self.certs else "ws") + "://" + portConfig.server_address + ":" + str(portConfig.server_https_port)
        
        self.accessTokenHasPermission = {}


    def addLogMessage(self, format_string, *args, **kwargs):
        self.eventLog.addLogMessage(self.getCurrentLogin(), format_string, *args, **kwargs)


    def getCurrentLogin(self):
        login = cherrypy.session.get('github_login', None)
        if login is None and self.is_authenticated():
            token = self.access_token()
            login = cherrypy.session['github_login'] = self.src_ctrl.getUserNameFromToken(token)
        return login or "Guest"


    def authenticate(self):
        auth_url = self.src_ctrl.authenticationUrl()

        if auth_url is not None:
            #stash the current url
            self.save_current_url()
            raise cherrypy.HTTPRedirect(auth_url)
        else:
            cherrypy.session['github_access_token'] = "DUMMY"


    def save_current_url(self):
        cherrypy.session['redirect_after_authentication'] = self.currentUrl()


    @staticmethod
    def is_authenticated():
        return 'github_access_token' in cherrypy.session


    @staticmethod
    def access_token():
        return cherrypy.session['github_access_token']


    def can_write(self):
        if not self.is_authenticated():
            return False

        token = self.access_token()
        is_authorized = self.accessTokenHasPermission.get(token)
        if is_authorized is None:
            is_authorized = self.src_ctrl.authorize_access_token(token)
            self.accessTokenHasPermission[token] = is_authorized

            self.addLogMessage(
                "Authorization: %s",
                "Granted" if is_authorized else "Denied"
                )
        return is_authorized


    def authorize(self, read_only):
        if not self.is_authenticated():
            # this redirects to the login page. Authorization will take place
            # again once the user is redirected back to the app.
            self.authenticate()
        else:
            if self.can_write():
                return

            message = (
                "You are not authorized to access this repository" if read_only else
                "You are not authorized to perform the requested operation"
                )

            raise cherrypy.HTTPError(403, message)


    @cherrypy.expose
    def logout(self):
        token = cherrypy.session.pop('github_access_token', None)
        if token and token in self.accessTokenHasPermission:
            del self.accessTokenHasPermission[token]

        cherrypy.session.pop('github_login')

        raise cherrypy.HTTPRedirect(self.address + "/")


    @cherrypy.expose
    def githubAuthCallback(self, code):
        # kept for backward compatibility
        return self.oauth_callback(code)


    @cherrypy.expose
    def oauth_callback(self, code):
        access_token = self.src_ctrl.getAccessTokenFromAuthCallbackCode(code)
        if not access_token:
            logging.error("Failed to accquire access token")
            raise cherrypy.HTTPError(401, "Unable to authenticate your session")

        logging.info("Access token is %s", access_token)

        cherrypy.session['github_access_token'] = access_token

        raise cherrypy.HTTPRedirect(
            cherrypy.session.pop('redirect_after_authentication', None) or self.address + "/"
            )


    def errorPage(self, errorMessage, currentRepo=None):
        return self.commonHeader(currentRepo=currentRepo) + "\n" + markdown.markdown("#ERROR\n\n" + errorMessage)


    @cherrypy.expose
    def index(self):
        raise cherrypy.HTTPRedirect(self.address + "/repos")


    @cherrypy.expose
    def test(self, testId):
        self.authorize(read_only=True)

        with self.testManager.database.view():
            testRun = self.testManager.getTestRunById(testId)

            if testRun is None:
                return self.errorPage("Unknown testid %s" % testId)

            grid = [["ARTIFACTS"]]

            if testRun.test.testDefinition.matches.Build:
                build_key = testRun.test.fullname.replace("/","_") + ".tar"

                if self.artifactStorage.build_exists(build_key):
                    grid.append([
                        HtmlGeneration.link(build_key, self.buildDownloadUrl(build_key))
                        ])
                else:
                    logging.info("No build found at %s", build_key)

            for artifactName in self.testResultKeys(testId):
                grid.append([
                    HtmlGeneration.link(
                        artifactName,
                        self.testResultDownloadUrl(testId, artifactName)
                        )
                    ])
            return (
                self.commonHeader(testRun.test.commitData.commit.repo) +
                markdown.markdown("# Test\n") +
                markdown.markdown("Test: %s\n" % testId) +
                markdown.markdown("## Commit ") + 
                self.commitLink(testRun.test.commitData.commit).render() + 
                markdown.markdown("##") + 
                self.testLogsButton(testId).render() + 
                markdown.markdown("## Artifacts\n") +
                (HtmlGeneration.grid(grid) if grid else "")
                )

    @cherrypy.expose
    def test_contents(self, testId, key):
        return self.processFileContents(self.artifactStorage.testContentsHtml(testId, key))

    def processFileContents(self, contents):
        if contents.matches.Redirect:
            raise cherrypy.HTTPRedirect(contents.url)

        if contents.content_type:
            cherrypy.response.headers['Content-Type'] = contents.content_type
        if contents.content_disposition:
            cherrypy.response.headers["Content-Disposition"] = contents.content_disposition
        if contents.content_encoding:
            cherrypy.response.headers["Content-Encoding"] = contents.content_encoding

        return contents.content

    def testLogsButton(self, testId):
        return HtmlGeneration.Link(
            self.testLogsUrl(testId),
            "LOGS", 
            is_button=True,
            button_style=self.disable_if_cant_write('btn-danger btn-xs')
            )

    def testLogsUrl(self, testId):
        return self.address + "/terminalForTest?testId=%s" % testId

    
    def testResultDownloadUrl(self, testId, key):
        return self.address + "/test_contents?testId=%s&key=%s" % (testId, key)

    def testResultKeys(self, testId):
        return self.artifactStorage.testResultKeysFor(testId)

    @cherrypy.expose
    def build_contents(self, key):
        return self.processFileContents(self.artifactStorage.buildContentsHtml(key))

    def buildDownloadUrl(self, key):
        return self.address + "/build_contents?key=%s" % key

    def commitLink(self, commit, failuresOnly=False, testName=None, textIsSubject=True):
        subject = "<not loaded yet>" if not commit.data else commit.data.subject

        if textIsSubject:
            text = subject if len(subject) < 71 else subject[:70] + '...'
        else:
            text = commit.repo.name + "/" + commit.hash[:8]
        
        extras = {}

        if failuresOnly:
            extras["failuresOnly"] = 'true'
        if testName:
            extras["testName"] = testName

        extras["repoName"] = commit.repo.name
        extras["commitHash"] = commit.hash

        return HtmlGeneration.link(
            text,
            self.address + "/commit" + ("?" if extras else "") + urllib.urlencode(extras),
            hover_text=subject
            )

    def branchLink(self, branch, testGroupsToExpand=None):
        return HtmlGeneration.link(branch.branchname, self.branchUrl(branch, testGroupsToExpand))

    def branchUrl(self, branch, testGroupsToExpand=None):
        args = {"reponame": branch.repo.name, "branchname": branch.branchname}
        if testGroupsToExpand:
            args["testGroupsToExpand"] = ",".join(testGroupsToExpand)
        return self.address + "/branch?" + urllib.urlencode(args)


    def disable_if_cant_write(self, style):
        if self.can_write() or "disabled" in style:
            return style
        return style + " disabled"


    def small_clear_button(self, url, label=None):
        label = label or "clear"
        return HtmlGeneration.Link(url,
                                   label,
                                   is_button=True,
                                   button_style=self.disable_if_cant_write('btn-danger btn-xs'))


    def clearBranchLink(self, branch):
        return self.small_clear_button(
            "/clearBranch?" + urllib.urlencode({'reponame': branch.repo.name, 'branchname':branch.branchname, 'redirect': self.redirect()}),
            )

    def clearCommitIdLink(self, commitId):
        return self.small_clear_button(
            "/clearCommit?" + urllib.urlencode({'commitId': commitId, 'redirect': self.redirect()}),
            )

    def sourceLinkForCommit(self, commit):
        url = self.src_ctrl.commit_url(commit.repo.name, commit.hash)
        if url:
            return HtmlGeneration.link(commit.hash[:7], url)
        else:
            return HtmlGeneration.lightGrey(commit.hash[:7])


    @cherrypy.expose
    def clearCommit(self, commitId, redirect):
        self.authorize(read_only=False)

        with self.testManager.database.view():
            self.testManager.clearCommitId(commitId)

        raise cherrypy.HTTPRedirect(redirect)

    @cherrypy.expose
    def clearBranch(self, branch, redirect=None):
        self.authorize(read_only=False)

        with self.testManager.database.view():
            commits = self.testManager.branches[branch].commits

            for c in commits:
                self.testManager.clearCommitId(c)

        if redirect is not None:
            raise cherrypy.HTTPRedirect(redirect)

    @cherrypy.expose
    def cancelTestRun(self, testRunId, redirect):
        self.authorize(read_only=False)
        
        with self.testManager.transaction_and_lock():
            testRun = self.testManager.getTestRunById(testRunId)

            if testRun is None:
                return self.errorPage("Unknown testid %s" % testRunId)

            if not testRun.canceled:
                self.testManager._cancelTestRun(testRun)

        raise cherrypy.HTTPRedirect(redirect)

    def cancelTestRunButton(self, testRunId):
        return HtmlGeneration.Link(
            self.address + "/cancelTestRun?" + urllib.urlencode({"testRunId":testRunId, "redirect": self.redirect()}),
            "cancel", 
            is_button=True,
            button_style=self.disable_if_cant_write('btn-danger btn-xs')
            )        
    
    @cherrypy.expose
    def machines(self):
        self.authorize(read_only=True)

        with self.testManager.database.view():
            machines = self.testManager.database.Machine.lookupAll(isAlive=True)

            grid = [["MachineID", "Hardware", "OS", "BOOTED AT", "UP FOR", "STATUS", "LASTMSG", "RUNNING"]]
            for m in sorted(machines, key=lambda m: -m.bootTime):
                row = []
                row.append(m.machineId)
                row.append(str(m.hardware))
                row.append(str(m.os))
                row.append(time.asctime(time.gmtime(m.bootTime)))
                row.append(secondsUpToString(time.time() - m.bootTime))
                
                if m.firstHeartbeat < 1.0:
                    row.append("BOOTING")
                else:
                    row.append("Heartbeat %s seconds ago" % int(time.time() - m.lastHeartbeat))
                
                row.append(m.lastHeartbeatMsg)

                tests = self.testManager.database.TestRun.lookupAll(runningOnMachine=m)
                deployments = self.testManager.database.Deployment.lookupAll(runningOnMachine=m)
                    
                if len(tests) + len(deployments) > 1:
                    row.append("ERROR: multiple test runs/deployments")
                elif tests:
                    row.append("TEST " + self.testRunLink(tests[0]).render() + self.cancelTestRunButton(tests[0]._identity).render())
                elif deployments:
                    d = deployments[0]
                    row.append(
                        "DEPLOYMENT " + self.commitLink(d.test.commitData.commit, textIsSubject=False).render() + self.connectDeploymentLink(d).render() 
                            + self.shutdownDeployment(d).render()
                        )
                
                grid.append(row)
                
            return self.commonHeader() + HtmlGeneration.grid(grid)


    @cherrypy.expose
    def commit(self, repoName, commitHash, failuresOnly=False, testName=None):
        self.authorize(read_only=True)

        with self.testManager.database.view():
            repo = self.testManager.database.Repo.lookupAny(name=repoName)
            if not repo:
                return self.errorPage("Repo %s doesn't exist" % repoName)

            commit = self.testManager.database.Commit.lookupAny(repo_and_hash=(repo, commitHash))
            if not commit:
                return self.errorPage("Commit %s/%s doesn't exist" % (repo, commitHash))

            if not commit.data:
                return self.errorPage("Commit hasn't been imported yet")

            testTypes = self.testManager.database.Test.lookupAll(commitData=commit.data)

            if testName is not None:
                testTypes = [x for x in testTypes if x.testDefinition.name == testName]

            tests = []
            for test in testTypes:
                tests.extend(self.testManager.database.TestRun.lookupAll(test=test))
            
            tests = sorted(tests, key=lambda test: -test.startedTimestamp)
            
            if failuresOnly:
                tests = [x for x in tests if not x.success and x.endTimestamp > 0.0]

            grid = self.gridForTestList_(tests, commit=commit, failuresOnly=failuresOnly)

            header = """## Commit `%s`: `%s`\n""" % (commit.hash[:10], commit.data.subject)
            
            if failuresOnly:
                header += "showing failures only. %s<br/><br/>" % \
                    self.commitLink(commit,
                                    False,
                                    testName).withTextReplaced("Show all test results").render()
            else:
                header += "showing both successes and failures. %s<br/><br/>" % \
                    self.commitLink(commit,
                                    True,
                                    testName).withTextReplaced("Show only failures").render()

            if testName:
                header += "showing only %s tests. %s<br/>" % (
                    testName,
                    self.commitLink(commit,
                                    failuresOnly,
                                    None).withTextReplaced("Show all tests").render()
                    )

            header = self.commonHeader(currentRepo=repoName) + markdown.markdown(header)

            buttons = []
            
            env_vals = [x.testDefinition for x in self.testManager.database.Test.lookupAll(commitData=commit.data)
                if x.testDefinition.matches.Deployment]

            if env_vals:
                buttons.append(HtmlGeneration.makeHtmlElement(markdown.markdown("#### Interactive Deployment Booters")))
                for env in sorted(env_vals, key=lambda e: e.name):
                    buttons.append(
                        HtmlGeneration.Link(self.bootTestOrEnvUrl(repoName + "/" + commitHash + "/" + env.name),
                           env.name,
                           is_button=True,
                           new_tab=True,
                           button_style=self.disable_if_cant_write('btn-danger btn-xs')
                           )
                        )
                    buttons.append(HtmlGeneration.makeHtmlElement("&nbsp;"*2))
                buttons.append(HtmlGeneration.makeHtmlElement("<br>"*2))

            test_vals = [x.testDefinition for x in self.testManager.database.Test.lookupAll(commitData=commit.data)
                if x.testDefinition.matches.Test or x.testDefinition.matches.Build]

            if test_vals:
                buttons.append(HtmlGeneration.makeHtmlElement(markdown.markdown("#### Interactive Test Booters")))
                for test in sorted(test_vals, key=lambda e: e.name):
                    buttons.append(
                        HtmlGeneration.Link(self.bootTestOrEnvUrl(repoName + "/" + commitHash + "/" + test.name),
                           test.name,
                           is_button=True,
                           new_tab=True,
                           button_style=self.disable_if_cant_write('btn-danger btn-xs')
                           )
                        )
                    buttons.append(HtmlGeneration.makeHtmlElement("&nbsp;"*2))
                buttons.append(HtmlGeneration.makeHtmlElement("<br>"*2))

            return header + HtmlGeneration.HtmlElements(buttons).render() + HtmlGeneration.grid(grid)

    def bootTestOrEnvUrl(self, fullname):
        return self.address + "/bootDeployment?" + urllib.urlencode({"fullname":fullname})

    @cherrypy.expose
    def bootDeployment(self, fullname):
        try:
            deploymentId = self.testManager.createDeployment(fullname, time.time())
        except Exception as e:
            logging.error("Failed to boot a deployment:\n%s", traceback.format_exc())
            return self.errorPage("Couldn't boot a deployment for %s: %s" % (fullname, str(e)))

        logging.info("Redirecting for %s", fullname)
        
        raise cherrypy.HTTPRedirect(self.address + "/terminalForDeployment?deploymentId=" + deploymentId)

    def testRunLink(self, testRun):
        return HtmlGeneration.link(str(testRun._identity)[:8], "/test?testId=" + testRun._identity)

    def gridForTestList_(self, sortedTests, commit=None, failuresOnly=False):
        grid = [["TEST", "TYPE", "STATUS", "LOGS", "STARTED", "MACHINE", "ELAPSED (MIN)",
                 "SINCE LAST HEARTBEAT (SEC)", "TOTAL TESTS", "FAILING TESTS"]]

        sortedTests = [x for x in sortedTests if not x.canceled]
        
        for testRun in sortedTests:
            row = []

            row.append(self.testRunLink(testRun))

            name = testRun.test.testDefinition.name

            if commit is None:
                row.append(name)
            else:
                row.append(
                    self.commitLink(commit, failuresOnly=failuresOnly, testName=name).withTextReplaced(name)
                    )

            if testRun.endTimestamp > 0.0:
                row.append("passed" if testRun.success else "failed")
            else:
                row.append(self.cancelTestRunButton(testRun._identity))

            row.append(self.testLogsButton(testRun._identity))

            row.append(time.ctime(testRun.startedTimestamp))

            if testRun.endTimestamp > 0.0:
                elapsed = (testRun.endTimestamp - testRun.startedTimestamp) / 60.0
            else:
                elapsed = (time.time() - testRun.startedTimestamp) / 60.0

            row.append(self.machineLink(testRun.machine))

            row.append("%.2f" % elapsed)

            if hasattr(testRun, "lastHeartbeat") and testRun.endTimestamp <= 0.0:
                timeSinceHB = time.time() - testRun.lastHeartbeat
            else:
                timeSinceHB = None

            row.append(str("%.2f" % timeSinceHB) if timeSinceHB is not None else "")

            if testRun.totalTestCount > 0:
                row.append(str(testRun.totalTestCount))
                row.append(str(testRun.totalFailedTestCount))
            else:
                row.append("")
                row.append("")

            grid.append(row)

        return grid

    @staticmethod
    def machineLink(machine):
        return HtmlGeneration.link(machine, "/machine?machineId="+machine.machineId)


    def login_link(self):
        self.save_current_url()
        return '<a href="%s">Login</a>' % self.src_ctrl.authenticationUrl()


    def logout_link(self):
        return ('<a href="/logout">'
                'Logout [%s] <span class="glyphicon glyphicon-user" aria-hidden="true"/>'
                '</a>') % self.getCurrentLogin()


    def commonHeader(self, currentRepo=None):
        headers = []
        headers.append(
            '<div align="right"><h5>%s</h5></div>' % (
                self.logout_link() if self.is_authenticated() else self.login_link())
            )

        nav_links = [
            ('Repos', '/repos'),
            ('Machines', '/machines'),
            ('Deployments', '/deployments')
            ]

        if currentRepo:
            if isinstance(currentRepo, unicode):
                reponame = str(currentRepo)
            elif isinstance(currentRepo, str):
                reponame = currentRepo
            else:
                reponame = currentRepo.name

            nav_links.append(("Branches", "/branches?" + urllib.urlencode({"repoName":reponame})))

        nav_links += [
            ('Activity Log', '/eventLogs')
            ]
        
        headers += ['<ul class="nav nav-pills">'] + [
            '<li role="presentation" class="{is_active}"><a href="{link}">{label}</a></li>'.format(
                is_active="active" if link == cherrypy.request.path_info else "",
                link=link,
                label=label)
            for label, link in nav_links
            ] + ['</ul>']
        return HtmlGeneration.headers + "\n" + "\n".join(headers)


    def toggleBranchUnderTestLink(self, branch):
        icon = "glyphicon-pause" if branch.isUnderTest else "glyphicon-play"
        hover_text = "%s testing this branch" % ("Pause" if branch.isUnderTest else "Start")
        button_style = "btn-xs " + ("btn-success active" if branch.isUnderTest else "btn-default")
        
        return HtmlGeneration.Link(
            "/toggleBranchUnderTest?" + 
                urllib.urlencode({'repo': branch.repo.name, 'branchname':branch.branchname, 'redirect': self.redirect()}),
            '<span class="glyphicon %s" aria-hidden="true"></span>' % icon,
            is_button=True,
            button_style=self.disable_if_cant_write(button_style),
            hover_text=hover_text
            )


    @cherrypy.expose
    def toggleBranchUnderTest(self, repo, branchname, redirect):
        self.authorize(read_only=False)

        with self.testManager.transaction_and_lock():
            branch = self.testManager.database.Branch.lookupOne(reponame_and_branchname=(repo, branchname))
            self.testManager.toggleBranchUnderTest(branch)

        raise cherrypy.HTTPRedirect(redirect)

    @cherrypy.expose
    def refresh(self, redirect=None):
        self.refreshBranches()
        raise cherrypy.HTTPRedirect(redirect or self.address + "/repos")

    def refreshBranches(self):
        self.testManager.markRepoListDirty(time.time())

    def redirect(self):
        qs = cherrypy.request.query_string

        return cherrypy.request.path_info + ("?" if qs else "") + qs

    def branchesGrid(self, repoName):
        t0 = time.time()
        with self.testManager.database.view():
            lock_time = time.time()
            repo = self.testManager.database.Repo.lookupOne(name=repoName)

            branches = self.testManager.database.Branch.lookupAll(repo=repo)
            
            refresh_button = HtmlGeneration.Link(
                "/refresh?" + urllib.urlencode({"redirect": self.redirect()}),
                '<span class="glyphicon glyphicon-refresh " aria-hidden="true" />',
                is_button=True,
                button_style='btn-default btn-xs',
                hover_text='Refresh branches'
                )

            grid = [["TEST", "BRANCH NAME", "COMMIT COUNT", refresh_button]]

            for branch in sorted(branches, key=lambda b:b.branchname):
                commits = self.testManager.commitsToDisplayForBranch(branch)

                row = []
                row.append(self.toggleBranchUnderTestLink(branch))
                row.append(self.branchLink(branch))
                row.append(str(len(commits)))

                if commits:
                    row.append(self.clearBranchLink(branch))

                grid.append(row)

            return grid

    @cherrypy.expose
    def deployments(self):
        self.authorize(read_only=True)

        grid = HtmlGeneration.grid(self.deploymentsGrid())
        
        return self.commonHeader() + grid

    def deploymentsGrid(self):
        with self.testManager.database.view():
            deployments = sorted(
                self.testManager.database.Deployment.lookupAll(isAlive=True),
                key=lambda d:d.createdTimestamp
                )
            
            grid = [["REPO", "COMMIT", "TEST", "BOOTED AT", "UP FOR", "CLIENTS", "", ""]]

            for d in deployments:
                row = []

                commit = d.test.commitData.commit
                repo = commit.repo

                row.append(
                    HtmlGeneration.link(repo.name, "/branches?" + urllib.urlencode({'repoName':repo.name}))
                    )

                row.append(
                    self.commitLink(commit)
                    )

                row.append(d.test.testDefinition.name)

                row.append(time.asctime(time.gmtime(d.createdTimestamp)))

                row.append(secondsUpToString(time.time() - d.createdTimestamp))

                row.append(str(self.testManager.streamForDeployment(d._identity).clientCount()))

                row.append(self.connectDeploymentLink(d))

                row.append(
                    HtmlGeneration.Link(
                        self.address + "/shutdownDeployment?deploymentId=" + d._identity,
                        "shutdown", 
                        is_button=True,
                        button_style=self.disable_if_cant_write('btn-danger btn-xs')
                        )
                    )

                grid.append(row)

            return grid

    def connectDeploymentLink(self, d):
        return HtmlGeneration.Link( 
            self.address + "/terminalForDeployment?deploymentId=" + d._identity,
            "connect",
            is_button=True,
            new_tab=True,
            button_style=self.disable_if_cant_write('btn-danger btn-xs')
            )

    def shutdownDeploymentLink(self, d):
        return HtmlGeneration.Link( 
            self.address + "/shutdownDeployment?deploymentId=" + d._identity,
            "shutdown",
            is_button=True,
            new_tab=True,
            button_style=self.disable_if_cant_write('btn-danger btn-xs')
            )

    @cherrypy.expose
    def shutdownDeployment(self, deploymentId):
        self.testManager.shutdownDeployment(str(deploymentId), time.time())

        raise cherrypy.HTTPRedirect(self.address + "/deployments")

    @cherrypy.expose
    def repos(self):
        self.authorize(read_only=True)

        grid = HtmlGeneration.grid(self.reposGrid())
        
        return self.commonHeader() + grid

    def reposGrid(self):
        with self.testManager.database.view():
            repos = self.testManager.database.Repo.lookupAll(isActive=True)
            repoNames = [r.name for r in repos]

            grid = [["REPO NAME", "BRANCH COUNT"]]

            for r in sorted(repoNames):
                branches = self.testManager.database.Branch.lookupAll(
                    repo=self.testManager.database.Repo.lookupOne(name=r)
                    )

                grid.append([
                    HtmlGeneration.link(r, "/branches?" + urllib.urlencode({'repoName':r})),
                    str(len(branches))
                    ])

            return grid

    @cherrypy.expose
    def branches(self, repoName):
        self.authorize(read_only=True)

        grid = HtmlGeneration.grid(self.branchesGrid(repoName))
        grid += HtmlGeneration.Link("/disableAllTargetedTests?" + 
                                        urllib.urlencode({'redirect': self.redirect()}),
                                    "Stop all drilling",
                                    is_button=True,
                                    button_style=self.disable_if_cant_write("btn-default")).render()

        return self.commonHeader(currentRepo=repoName) + grid


    @cherrypy.expose
    def disableAllTargetedTests(self, redirect):
        self.authorize(read_only=False)

        with self.testManager.database.view():
            for branch in self.testManager.branches.itervalues():
                branch.setTargetedTestList([])
                branch.setTargetedCommitIds([])

        raise cherrypy.HTTPRedirect(redirect)

    def toggleBranchTargetedTestListLink(self, branch, testType, testGroupsToExpand):
        is_drilling = False #testType in branch.targetedTestList()
        icon = "glyphicon-minus" if is_drilling else "glyphicon-plus"
        hover_text = "Run less of this test" if is_drilling else "Run more of this test"
        button_style = "btn-default btn-xs" + (" active" if is_drilling else "")
        return HtmlGeneration.Link(
            "/toggleBranchTestTargeting?" + urllib.urlencode({
                    "repo": branch.repo.name, 
                    "branchname": branch.branchname,
                    "testType": testType,
                    "testGroupsToExpand": ",".join(testGroupsToExpand)
                }),
            '<span class="glyphicon %s" aria-hidden="true"></span>' % icon,
            is_button=True,
            button_style=self.disable_if_cant_write(button_style),
            hover_text=hover_text
            )

    def toggleBranchTargetedCommitIdLink(self, branch, commit):
        is_drilling = False

        icon = "glyphicon-minus" if is_drilling else "glyphicon-plus"
        hover_text = "Run less of this commit" if is_drilling else "Run more of this commit"
        button_style = "btn-default btn-xs" + (" active" if is_drilling else "")
        return HtmlGeneration.Link(
                "/toggleBranchCommitTargeting?" + urllib.urlencode({
                    "repo": branch.repo.name, 
                    "branchname": branch.branchname,
                    "commitHash": commit.hash
                }),
                '<span class="glyphicon %s" aria-hidden="true"></span>' % icon,
                is_button=True,
                button_style=self.disable_if_cant_write(button_style),
                hover_text=hover_text
                )

    @cherrypy.expose
    def toggleBranchTestTargeting(self, reponame, branchname, testType, testGroupsToExpand):
        self.authorize(read_only=False)

        with self.testManager.database.view():
            branch = self.testManager.database.Branch.lookupOne(reponame_and_branchname=(reponame, branchname))

            if testType in branch.targetedTestList():
                branch.setTargetedTestList(
                    [x for x in branch.targetedTestList() if x != testType]
                    )
            else:
                branch.setTargetedTestList(
                    branch.targetedTestList() + [testType]
                    )

        raise cherrypy.HTTPRedirect(self.branchUrl(branch))


    @cherrypy.expose
    def toggleBranchCommitTargeting(self, reponame, branchname, commitHash):
        self.authorize(read_only=False)

        with self.testManager.database.view():
            branch = self.testManager.database.Branch.lookupOne(reponame_and_branchname=(reponame, branchname))

            if commitHash in branch.targetedCommitIds():
                logging.warn("set to off")
                branch.setTargetedCommitIds(
                    [x for x in branch.targetedCommitIds() if x != commitId]
                    )
            else:
                logging.warn("set to on")
                branch.setTargetedCommitIds(
                    branch.targetedCommitIds() + [commitId]
                    )

        raise cherrypy.HTTPRedirect(self.branchUrl(branch))

    @staticmethod
    def errRateVal(testCount, successCount):
        if testCount == 0:
            return 0

        successCount = float(successCount)

        toReturn = 1.0 - successCount / testCount
        return toReturn


    @cherrypy.expose
    def branch(self, reponame, branchname, testGroupsToExpand=None):
        self.authorize(read_only=True)

        t0 = time.time()
        with self.testManager.database.view():
            branch = self.testManager.database.Branch.lookupAny(reponame_and_branchname=(reponame,branchname))

            if branch is None:
                return self.errorPage("Branch %s/%s doesn't exist" % (reponame, branchname))

            return self.testPageForCommits(
                reponame,
                self.testManager.commitsToDisplayForBranch(branch), 
                "Branch `" + branch.branchname + "`", 
                testGroupsToExpand, 
                branch
                )

    def testPageForCommits(self, reponame, commits, headerText, testGroupsToExpand, branch):
        ungroupedUniqueTestIds = sorted(set(t.testDefinition.name for c in commits for t in self.testManager.database.Test.lookupAll(commitData=c.data)))

        testGroupsToTests = {}
        for testName in ungroupedUniqueTestIds:
            group = testName.split("/")[0]
            if group not in testGroupsToTests:
                testGroupsToTests[group] = []
            testGroupsToTests[group].append(testName)

        testGroupsToExpand = [] if testGroupsToExpand is None else testGroupsToExpand.split(",")

        for group in list(testGroupsToTests):
            if len(testGroupsToTests[group]) == 1:
                testGroupsToExpand.append(group)

        def appropriateGroup(testName):
            groupPrefix = testName.split("/")[0]
            if groupPrefix in testGroupsToExpand:
                return testName

            return groupPrefix

        testGroups = sorted(list(set(appropriateGroup(x) for x in ungroupedUniqueTestIds)))
        grid = self.createGridForBranch(branch,
                                        testGroups,
                                        ungroupedUniqueTestIds,
                                        testGroupsToExpand)

        commit_string = ""
        detail_divs = ""

        ids_to_resize = []

        branches = {}

        commits = [c for c in commits if c.data]

        commit_hashes = {c.hash: c for c in commits}
        children = {c.hash: [] for c in commits}
        parents = {}

        for c in commits:
            parents[c.hash] = [p.hash for p in c.data.parents if p.hash in commit_hashes]
            for p in parents[c.hash]:
                children[p].append(c.hash)
        
        for c in commits:
            if not parents[c.hash]:
                branchname = "branch_%s" % len(branches)

                commit_string += 'var %s = gitgraph.branch("%s");\n' % (branchname, branchname)
                branches[c.hash] = branchname

        #we need to walk the commits from bottom to top. E.g. the ones with no parents go first.
        order = {}
        unordered_parents = {h: set(parents[h]) for h in parents}
        edges = [h for h in unordered_parents if not unordered_parents[h]]

        while len(order) < len(commits):
            e = edges.pop()

            order[e] = max([order[p]+1 for p in parents[e]] + [0])

            for c in children[e]:
                unordered_parents[c].discard(e)
                if not unordered_parents[c]:
                    edges.append(c)

        commits = sorted(commits, key=lambda c: order[c.hash])

        for commit_ix, c in enumerate(commits):
            commit_string +=  "//%s -- %s\n" % (commit_ix, c.hash)

            parentsWeHave = parents[c.hash]

            if len(parentsWeHave) == 0:
                #push a commit onto the branch
                our_branch = branches[c.hash]

                commit_string += "%s.commit({sha1: '%s', message: '%s', detailId: 'commit_%s'});\n" % (
                    branches[c.hash],
                    c.hash, 
                    c.data.subject.replace("'", "\\'"),
                    c.hash
                    )

            elif len(parentsWeHave) == 1:
                #push a commit onto the branch
                our_branch = branches[(parentsWeHave[0], c.hash)]

                commit_string += "%s.commit({sha1: '%s', message: '%s', detailId: 'commit_%s'});\n" % (
                    our_branch,
                    c.hash, 
                    c.data.subject.replace("'", "\\'"),
                    c.hash
                    )
            else:
                our_branch = branches[(parentsWeHave[0], c.hash)]
                other_branch = branches[(parentsWeHave[1], c.hash)]

                commit_string += "%s.merge(%s, {sha1: '%s', message: '%s', detailId: 'commit_%s'}).delete();" % (other_branch, our_branch, 
                    c.hash, 
                    c.data.subject.replace("'", "\\'"),
                    c.hash
                    )

            if len(children[c.hash]) == 0:
                #nothing to do - this is terminal
                pass
            elif len(children[c.hash]) == 1:
                #one child gets to use this branch
                branches[(c.hash, children[c.hash][0])] = our_branch
            else:
                #this is a fork - one child gets to use the branch, and everyone else needs to get a fork
                branches[(c.hash, children[c.hash][0])] = our_branch
                for other_child in children[c.hash][1:]:
                    branchname = "branch_%s" % len(branches)

                    commit_string += 'var %s = %s.branch("%s");\n' % (branchname, our_branch, branchname)

                    branches[(c.hash, other_child)] = branchname


        for c in reversed(commits):
            gridrow = self.getBranchCommitRow(branch,
                                          c,
                                          testGroups,
                                          ungroupedUniqueTestIds,
                                          testGroupsToTests)

            grid.append(gridrow)

        header = (
            markdown.markdown("# " + headerText) + "\n\n" +
            '<p>Click the <span class="glyphicon glyphicon-plus" aria-hidden="true"></span>/'
            '<span class="glyphicon glyphicon-minus" aria-hidden="true"></span> buttons '
            'to increase/decrease the amount of testing on a given commit or test suite. '
            'If both a test suite and a commit are selected within a branch'
            ", only the cross section will received extra test coverage.</p><br>"
            )
        
        grid = HtmlGeneration.grid(grid, header_rows=2, rowHeightOverride=33)
        
        canvas = HtmlGeneration.gitgraph_canvas_setup(commit_string, grid)

        return self.commonHeader(reponame) + header + detail_divs + canvas


    @staticmethod
    def gitGraph(depth, symbol):
        return HtmlGeneration.BoldTag(
            symbol if depth == 0 else
            (HtmlGeneration.whitespace*2).join('|' for _ in xrange(depth)) +
            HtmlGeneration.whitespace*2 + symbol
            )


    @staticmethod
    def currentUrl(remove_query_params=None):
        if remove_query_params is None:
            return cherrypy.url(qs=cherrypy.request.query_string).replace('http://', 'https://')

        query_string = cherrypy.lib.httputil.parse_query_string(
            cherrypy.request.query_string
            )
        return cherrypy.url(
            qs="&".join("%s=%s" % (k, v)
                        for k, v in query_string.iteritems()
                        if k not in remove_query_params)
            ).replace('http://', 'https://')


    def createGridForBranch(self,
                            branch,
                            testGroups,
                            ungroupedUniqueTestIds,
                            testGroupsToExpand):
        testHeaders = []
        testGroupExpandLinks = []
        for testGroup in testGroups:
            testGroupPrefix = testGroup.split(".")[0]

            if testGroup in ungroupedUniqueTestIds:
                if branch:
                    testHeaders.append(
                        self.toggleBranchTargetedTestListLink(branch,
                                                              testGroup,
                                                              ",".join(testGroupsToExpand))
                        )
                else:
                    testHeaders.append("")

                if branch:
                    if testGroupPrefix in testGroupsToExpand:
                        testGroupExpandLinks.append(
                            HtmlGeneration.link(
                                testGroupPrefix,
                                self.branchUrl(branch, [x for x in testGroupsToExpand if x != testGroupPrefix])
                                ) + (
                                    "." + testGroup[len(testGroupPrefix)+1:] if testGroup != testGroupPrefix else ""
                                )
                            )
                    else:
                        testGroupExpandLinks.append(testGroup)
                else:
                    testGroupExpandLinks.append(testGroup)
            else:
                testHeaders.append("")
                if branch:
                    testGroupExpandLinks.append(
                        HtmlGeneration.link(
                            testGroup,
                            self.branchUrl(branch, testGroupsToExpand + [testGroup])
                            )
                        )
                else:
                    testGroupExpandLinks.append(testGroup)
                
            testGroupExpandLinks[-1] = HtmlGeneration.pad(testGroupExpandLinks[-1], 20)
            testHeaders[-1] = HtmlGeneration.pad(testHeaders[-1], 20)

        grid = [["", "", "", ""] + testHeaders + ["", "", ""]]
        grid.append(
            ["COMMIT", "", "(running)"] + \
            testGroupExpandLinks + \
            ["SOURCE", "", "UPSTREAM", "DOWNSTREAM"]
            )
        return grid


    def getBranchCommitRow(self,
                           branch,
                           commit,
                           testGroups,
                           ungroupedUniqueTestIds,
                           testGroupsToTests):
        def anyTestInGroupIsTargetedInCommit(commit, testGroup):
            return False

            for group in testGroupsToTests[testGroup]:
                if commit.isTargetedTest(group):
                    return True
            return False

        def allTestsInGroupAreTargetedInCommit(commit, testGroup):
            return False

            for group in testGroupsToTests[testGroup]:
                if not commit.isTargetedTest(group):
                    return False
            return True

        row = [self.commitLink(commit)]

        if branch:
            row.append(self.toggleBranchTargetedCommitIdLink(branch, commit))

        row.append(self.testManager.totalRunningCountForCommit(commit) or "tests not enabled" if not commit.priority else "")

        if commit.data:
            tests = {t.testDefinition.name: t for t in self.testManager.database.Test.lookupAll(commitData=commit.data)}
        else:
            tests = {}

        class Stat:
            def __init__(self, message, totalRuns, runningCount, passCount, failCount, errRate):
                self.totalRuns = totalRuns
                self.runningCount = runningCount
                self.passCount = passCount
                self.failCount = failCount
                self.message = message
                self.errRate = errRate
                if errRate is None and self.totalRuns:
                    self.errRate = 1.0 - self.passCount / float(self.totalRuns)

            def __add__(self, other):
                if self.message == other.message:
                    message = self.message
                else:
                    message = ""

                if self.totalRuns == 0 or self.errRate is None:
                    blendedErrRate = other.errRate
                elif other.totalRuns == 0 or other.errRate is None:
                    blendedErrRate = self.errRate
                else:
                    blendedErrRate = 1.0 - (1.0 - other.errRate) * (1.0 - self.errRate)

                return Stat(
                    self.message,
                    None,
                    self.runningCount + other.runningCount,
                    None,
                    None,
                    blendedErrRate
                    )

        def computeStatForTestGroup(testGroup):
            if testGroup in ungroupedUniqueTestIds:
                if testGroup not in tests:
                    return Stat("", 0,0,0,0,None)

                test = tests[testGroup]
                if test.priority.matches.UnresolvedDependencies:
                    message="Unresolved Dependencies"
                elif test.priority.matches.DependencyFailed:
                    message="Dependency Failed"
                else:
                    message=""

                return Stat(
                    message,
                    test.totalRuns,
                    test.activeRuns,
                    test.successes,
                    test.totalRuns - test.successes,
                    None
                    )

            grp = [u for u in ungroupedUniqueTestIds if u.startswith(testGroup+"/")]

            s = computeStatForTestGroup(grp[0])
            for g in grp[1:]:
                s = s + computeStatForTestGroup(g)
            return s

        for testGroup in testGroups:
            stat = computeStatForTestGroup(testGroup)

            if stat.errRate is None:
                if stat.runningCount == 0:
                    row.append(stat.message)
                else:
                    row.append("[%s running]" % stat.runningCount)
            else:
                errRate = self.errRateAndTestCount(stat.errRate, stat.totalRuns)

                #check if this point in the commit-sequence has a statistically different
                #probability of failure from its peers and mark it if so.

                if stat.failCount and testGroup in ungroupedUniqueTestIds:
                    row.append(
                        self.commitLink(commit,
                                        failuresOnly=True,
                                        testName=testGroup).withTextReplaced(errRate)
                        )
                else:
                    row.append(HtmlGeneration.lightGrey(errRate))

            if testGroup in ungroupedUniqueTestIds:
                if False: #commit.isTargetedTest(testGroup):
                    row[-1] = HtmlGeneration.blueBacking(row[-1])
            else:
                if allTestsInGroupAreTargetedInCommit(commit, testGroup):
                    row[-1] = HtmlGeneration.blueBacking(row[-1])

                if anyTestInGroupIsTargetedInCommit(commit, testGroup):
                    row[-1] = HtmlGeneration.lightGreyBacking(row[-1])

        row.append(self.sourceLinkForCommit(commit))
        
        row.append(
            HtmlGeneration.lightGrey("waiting to load tests") 
                    if not commit.data
            else HtmlGeneration.lightGrey("invalid test file") 
                    if commit.data.testDefinitionsError
            else self.clearCommitIdLink(commit)
            )

        upstream = self.testManager.upstreamCommits(commit)
        downstream = self.testManager.downstreamCommits(commit)
        row.append(",&nbsp;".join([self.commitLink(c, textIsSubject=False).render() for c in upstream[:5]]))
        if len(upstream) > 5:
            row[-1] += ",&nbsp;..."

        row.append(",".join([self.commitLink(c, textIsSubject=False).render() for c in downstream[:5]]))
        if len(downstream) > 5:
            row[-1] += ",&nbsp;..."


        return row

    @staticmethod
    def errRateAndTestCount(errRate, testCount):
        if errRate == 0.0:
            if testCount:
                return "%4s@%3s%s" % (testCount, 0, "%")
            else:
                return "0%"

        if errRate < 0.01:
            errRate *= 10000
            errText = '.%2s' % int(errRate)
        elif errRate < 0.1:
            errRate *= 100
            errText = '%s.%s' % (int(errRate), int(errRate * 10) % 10)
        else:
            errRate *= 100
            errText = '%3s' % int(errRate)

        return "%4s@%3s" % (testCount, errText) + "%"


    @staticmethod
    def errRate(frac):
        tr = "%.1f" % (frac * 100) + "%"
        tr = tr.rjust(6)

        if frac < .1:
            tr = HtmlGeneration.lightGrey(tr)

        if frac > .9:
            tr = HtmlGeneration.red(tr)

        return tr


    @cherrypy.expose
    def eventLogs(self):
        self.authorize(read_only=True)
        return self.commonHeader() + self.generateEventLogHtml(1000)


    def generateEventLogHtml(self, maxMessageCount=10):
        messages = self.eventLog.getTopNLogMessages(maxMessageCount)

        return markdown.markdown("## Most recent actions:\n\n") + HtmlGeneration.grid(
            [["Date", "user", "Action"]] +
            [[msg["date"], msg["user"], msg["message"]] for msg in reversed(messages)]
            )

    @cherrypy.expose
    def githubReceivedAPush(self):
        return self.webhook()

    @cherrypy.expose
    def webhook(self, *args, **kwds):
        if 'Content-Length' not in cherrypy.request.headers:
            raise cherrypy.HTTPError(400, "Missing Content-Length header")

        if cherrypy.request.headers['Content-Type'] == "application/x-www-form-urlencoded":
            payload = simplejson.loads(cherrypy.request.body_params['payload'])
        else:
            payload = simplejson.loads(cherrypy.request.body.read(int(cherrypy.request.headers['Content-Length'])))

        event = self.src_ctrl.verify_webhook_request(cherrypy.request.headers, payload)

        if not event:
            logging.error("Invalid webhook request")
            raise cherrypy.HTTPError(400, "Invalid webhook request")

        #don't block the webserver itself, so we can do this in a background thread
        self.refreshBranches()


    @cherrypy.expose
    def interactive_socket(self, **kwargs):
        pass

    @cherrypy.expose
    def terminalForTest(self, testId):
        return self.websocketText(urllib.urlencode({"testId":testId}))

    @cherrypy.expose
    def terminalForDeployment(self, deploymentId):
        return self.websocketText(urllib.urlencode({"deploymentId":deploymentId}))

    @cherrypy.expose
    def machineHeartbeatMessage(self, machineId, heartbeatmsg):
        self.testManager.machineHeartbeat(machineId, time.time(), heartbeatmsg)

    def websocketText(self, urlQuery):
        return """
        <!doctype html>
        <html lang="en">

        <head>
            <meta charset="UTF-8">
            <title>TestLooper Interactive</title>
            <script src="/js/hterm_all.js"></script>
            <script>
            var term;
            var websocket;
            var address = "__websocket_address__";

            if (window.WebSocket) { 
                websocket = new WebSocket(address, ['protocol']); 
            }
            else if (window.MozWebSocket) {
                websocket = MozWebSocket(address);
            }
            else {
                console.log('WebSocket Not Supported');
            }

            var buf = '';

            function Terminal(argv) {
                this.argv_ = argv;
                this.io = null;
                this.pid_ = -1;
            }

            Terminal.prototype.run = function() {
                this.io = this.argv_.io.push();

                this.io.onVTKeystroke = this.sendString_.bind(this);
                this.io.sendString = this.sendString_.bind(this);
                this.io.onTerminalResize = this.onTerminalResize.bind(this);
            }

            Terminal.prototype.sendString_ = function(str) {
                websocket.send(str);
            };

            Terminal.prototype.onTerminalResize = function(col, row) {
                //socket.emit('resize', { col: col, row: row });
            };

            websocket.onopen = function() {
                lib.init(function() {
                    hterm.defaultStorage = new lib.Storage.Local();
                    term = new hterm.Terminal();
                    window.term = term;
                    term.decorate(document.getElementById('terminal'));

                    term.setCursorPosition(0, 0);
                    term.setCursorVisible(true);
                    term.prefs_.set('ctrl-c-copy', true);
                    term.prefs_.set('ctrl-v-paste', true);
                    term.prefs_.set('use-default-window-copy', true);

                    term.runCommandClass(Terminal, document.location.hash.substr(1));
                    
                    websocket.send(" ")

                    /*
                    socket.emit('resize', {
                        col: term.screenSize.width,
                        row: term.screenSize.height
                    });
                    */

                    if (buf && buf != '')
                    {
                        term.io.writeUTF16(buf);
                        buf = '';
                    }
                });
            };

            websocket.onmessage = function(data) {
                if (!term) {
                    buf += data.data;
                    return;
                }
                term.io.writeUTF16(data.data);
            };
            
            </script>
            <style>
                html,
                body {
                    height: 100%;
                    width: 100%;
                    margin: 0px;
                }
                #terminal {
                    display: block;
                    position: relative;
                    width: 100%;
                    height: 100%;
                }
            </style>
        </head>

        <body>
            <div id="terminal"></div>
        </body>

        </html>
        """.replace("__websocket_address__", self.websocket_address + "/interactive_socket?" + urlQuery)

    def start(self):
        config = {
            'global': {
                "engine.autoreload.on":False,
                'server.socket_host': '0.0.0.0',
                'server.socket_port': self.httpPort,
                'server.show_tracebacks': False,
                'request.show_tracebacks': False,
                'tools.sessions.on': True,
                }
            }

        if self.certs:
            config['global'].update({
                'server.ssl_module':'pyopenssl',
                'server.ssl_certificate':self.certs.cert,
                'server.ssl_private_key':self.certs.key,
                'server.ssl_certificate_chain':self.certs.chain
                })

        cherrypy.config.update(config)
        
        cherrypy.tools.websocket = WebSocketTool()

        logging.info("STARTING HTTP SERVER")

        current_dir = os.path.dirname(__file__)
        path_to_source_root = os.path.abspath(os.path.join(current_dir, "..", ".."))

        temp_dir_for_tarball = tempfile.mkdtemp()

        SubprocessRunner.callAndAssertSuccess(
            ["tar", "cvfz", os.path.join(temp_dir_for_tarball, "test_looper.tar.gz"), 
                "--directory", path_to_source_root, "test_looper"
            ])

        with DirectoryScope.DirectoryScope(path_to_source_root):
            SubprocessRunner.callAndAssertSuccess(
                ["zip", "-r", os.path.join(temp_dir_for_tarball, "test_looper.zip"), "test_looper", "-x", "*.pyc", "*.js"]
                )

        with DirectoryScope.DirectoryScope(temp_dir_for_tarball):
            SubprocessRunner.callAndReturnOutput(
                ["curl", "https://bootstrap.pypa.io/get-pip.py", "-O", os.path.join(temp_dir_for_tarball, "get-pip.py")]
                )
            assert os.path.exists(os.path.join(temp_dir_for_tarball, "get-pip.py"))

        cherrypy.tree.mount(self, '/', {
            '/favicon.ico': {
                'tools.staticfile.on': True,
                'tools.staticfile.filename': os.path.join(current_dir,
                                                          'content',
                                                          'favicon.ico')
                },
            '/get-pip.py': {
                'tools.staticfile.on': True,
                'tools.staticfile.filename': os.path.join(temp_dir_for_tarball,
                                                          'get-pip.py')
                },
            '/test_looper.tar.gz': {
                'tools.staticfile.on': True,
                'tools.staticfile.filename': os.path.join(temp_dir_for_tarball,
                                                          'test_looper.tar.gz')
                },
            '/test_looper.zip': {
                'tools.staticfile.on': True,
                'tools.staticfile.filename': os.path.join(temp_dir_for_tarball,
                                                          'test_looper.zip')
                },
            '/css': {
                'tools.staticdir.on': True,
                'tools.staticdir.dir': os.path.join(current_dir, 'css')
                },
            '/js': {
                'tools.staticdir.on': True,
                'tools.staticdir.dir': os.path.join(current_dir, 'content', 'js')
                },
            '/interactive_socket': {
                'tools.websocket.on': True, 
                'tools.websocket.handler_cls': MakeWebsocketHandler(self),
                'tools.websocket.protocols': ['protocol']
                }
            })

        cherrypy.server.socket_port = self.httpPort

        cherrypy.engine.autoreload.on = False

        cherrypy.engine.signals.subscribe()

        WebSocketPlugin(cherrypy.engine).subscribe()

        cherrypy.engine.start()

    @staticmethod
    def stop():
        logging.info("Stopping cherrypy engine")
        cherrypy.engine.exit()
        logging.info("Cherrypy engine stopped")
