import test_looper.server.rendering.Context as Context
import test_looper.server.rendering.ComboContexts as ComboContexts
import test_looper.server.rendering.TestSummaryRenderer as TestSummaryRenderer
import test_looper.server.HtmlGeneration as HtmlGeneration
import test_looper.core.algebraic_to_json as algebraic_to_json
import time
import cgi

octicon = HtmlGeneration.octicon
card = HtmlGeneration.card

class TestContext(Context.Context):
    def __init__(self, renderer, test, options):
        Context.Context.__init__(self, renderer, options)
        self.test = test
        self.commit = self.testManager.oldestCommitForTest(test)
        self.repo = self.commit.repo
        self.testName = test.testDefinition.name
        
    def consumePath(self, path):
        if path and path[0] == "test":
            items, remainder = self.popToDash(path[1:])

            return self.renderer.contextFor(ComboContexts.IndividualTest(self.test, "/".join(items)), self.options), remainder

        if path:
            testRun = self.database.TestRun(path[0])
            if testRun.exists():
                return self.renderer.contextFor(testRun, self.options), path[1:]

        return None, path

    def renderBreadcrumbPrefixes(self):
        return ["Suites" if self.test.testDefinition.matches.Test else "Builds"]

    def primaryObject(self):
        return self.test

    def urlBase(self):
        prefix = "repos/" + self.repo.name + "/-/commits/"
        return prefix + self.commit.hash + "/tests/" + self.testName

    def renderLink(self, includeCommit=True):
        if includeCommit:
            res = self.contextFor(self.commit).renderLink() + "/"
        else:
            res = ''

        return res + HtmlGeneration.link(self.testName, self.urlString())

    def bootTestOrEnvUrl(self):
        return "/bootDeployment?testHash=" + self.test.hash

    def contextViews(self):
        return ["test_runs", "test_dependencies", "test_definition"]

    def renderViewMenuItem(self, view):
        if view == "test_runs":
            return "Runs"
        if view == "test_definition":
            return "Test Definition"
        if view == "test_dependencies":
            return "Dependencies"
        return view

    def renderViewMenuMouseoverText(self, view):
        if view == "test_runs":
            return "All runs of this individual suite"
        if view == "test_definition":
            return "A view of the actual test definition used by the looper"
        if view == "test_dependencies":
            return "Info on the dependencies this test has"
        return ""

    def renderPageBody(self):
        test = self.test

        if self.currentView() == "test_runs":
            testRuns = self.testManager.database.TestRun.lookupAll(test=test)

            if not testRuns:
                return card("No runs of this test")

            return HtmlGeneration.grid(self.gridForTestList_(testRuns))

        if self.currentView() == "test_definition":
            return card(
                '<pre class="language-yaml"><code class="line-numbers">%s</code></pre>' % cgi.escape(
                    algebraic_to_json.encode_and_dump_as_yaml(test.testDefinition)
                    )
                )

        if self.currentView() == "test_dependencies":
            grid = self.allTestDependencyGrid()
            if not grid:
                return card("No dependencies")
            return HtmlGeneration.grid(grid)

    def allTestDependencyGrid(self):
        grid = [["COMMIT", "TEST", ""]]

        for subtest in self.testManager.allTestsDependedOnByTest(self.test):
            grid.append([
                self.contextFor(self.testManager.oldestCommitForTest(subtest)).renderLink(),
                self.contextFor(subtest).renderLink(),
                TestSummaryRenderer.TestSummaryRenderer([subtest], testSummaryUrl="").renderSummary()
                ])

        return grid

    def gridForTestList_(self, sortedTests):
        grid = [["TEST RUN", "TYPE", "STATUS", "LOGS", "CLEAR", "STARTED", "ELAPSED (MIN)",
                 "SINCE LAST HEARTBEAT (SEC)", "TOTAL TESTS", "FAILING TESTS"]]

        sortedTests = [x for x in sortedTests if not x.canceled]
        
        for testRun in sortedTests:
            row = []

            row.append(self.contextFor(testRun).renderLink(False, False))

            name = testRun.test.testDefinition.name

            row.append(name)

            if testRun.endTimestamp > 0.0:
                row.append("passed" if testRun.success else "failed")
            else:
                row.append(self.renderer.cancelTestRunButton(testRun._identity))

            row.append(self.renderer.testLogsButton(testRun._identity))

            row.append(self.renderer.deleteTestRunButton(testRun._identity))

            row.append(time.ctime(testRun.startedTimestamp))

            if testRun.endTimestamp > 0.0:
                elapsed = (testRun.endTimestamp - testRun.startedTimestamp) / 60.0
            else:
                elapsed = (time.time() - testRun.startedTimestamp) / 60.0

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

    def childContexts(self, currentChild):
        return []

    def parentContext(self):
        return self.contextFor(
            ComboContexts.CommitAndConfiguration(
                commit=self.commit, 
                configurationName=self.testManager.configurationForTest(self.test)
                )
            )

    def iconType(self):
        if self.test.testDefinition.matches.Build:
            return "tools"
        else:
            return "beaker"

    def renderMenuItemText(self, isHeader):
        return (octicon(self.iconType()) if isHeader else "") + self.testName

    def renderNavbarLink(self):
        return self.renderLink(includeCommit=False)

        
