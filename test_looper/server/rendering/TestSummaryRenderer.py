import test_looper.server.HtmlGeneration as HtmlGeneration
import cgi

octicon = HtmlGeneration.octicon


def convertToIntIfClose(x):
    if abs(x - round(x, 0)) < .01:
        return int(round(x, 0))
    return x



def cached(f):
    def function(self):
        cname = '_cache' + f.__name__
        if cname in self.__dict__:
            return self.__dict__[cname]
        else:
            self.__dict__[cname] = f(self)
        return self.__dict__[cname]

    return function

class TestSummaryRenderer:
    """Class for rendering a specific set of tests."""
    def __init__(self, tests, testSummaryUrl=None):
        self.tests = tests
        self.url = testSummaryUrl

    @cached
    def allBuilds(self):
        return [t for t in self.tests if t.testDefinition.matches.Build]

    @cached
    def allTests(self):
        return [t for t in self.tests if t.testDefinition.matches.Test]

    @cached
    def allEnvironments(self):
        envs = set()
        for t in self.tests:
            envs.add(t.testDefinition.environment)
        return envs

    @cached
    def hasOneEnvironment(self):
        return len(self.allEnvironments()) == 1

    def renderSummary(self):
        #first, see whether we have any tests
        if not self.tests or not self.allEnvironments():
            return ""

        button_text = self.renderSingleEnvironment()

        active = sum(t.activeRuns for t in self.tests)
        if active:
            button_text = '<span class="pr-1">%s</span>' % button_text
            button_text += '<span class="badge badge-info pl-1">{workers}{icon}</span>'.format(workers=max(active,0), icon=octicon("pulse"))

        summary = self.tooltipSummary()

        if summary:
            summary = "<div>%s</div>" % summary

        if active:
            summary += "<div>%s active jobs</div>" % active

        if summary:
            if self.url:
                button_text = (
                    '<div onclick="location.href=\'{url}\';" class="clickable-div" data-toggle="tooltip" title="{summary}" data-html="true">{text}</div>'
                        .format(summary=cgi.escape(summary), text=button_text,url=self.url)
                    )
            else:
                button_text = (
                    '<span data-toggle="tooltip" title="{summary}" data-html="true">{text}</span>'
                        .format(summary=cgi.escape(summary), text=button_text)
                    )

        elif self.url:
            button_text = (
                '<div onclick="location.href=\'{url}\';" class="clickable-div" title="{summary}" data-html="true">{text}</div>'
                    .format(summary=cgi.escape(summary), text=button_text,url=self.url)
                )
        
        return button_text


    def categorizeAllBuilds(self):
        goodBuilds = []
        badBuilds = []
        waitingBuilds = []

        builds = self.allBuilds()
        for b in builds:
            category = self.categorizeBuild(b)
            if category == "OK":
                goodBuilds += [b]
            if category == "BAD":
                badBuilds += [b]
            if category == "PENDING":
                waitingBuilds += [b]

        return goodBuilds,badBuilds,waitingBuilds

    def tooltipSummary(self):
        #first, see if all of our builds have completed
        goodBuilds,badBuilds,waitingBuilds = self.categorizeAllBuilds()

        if badBuilds:
            return "Builds failed: " + ", ".join([b.testDefinition.name for b in badBuilds])

        if waitingBuilds:
            if (waitingBuilds[0].commitData.commit.userPriority == 0 and 
                    waitingBuilds[0].commitData.commit.calculatedPriority == 0):
                return "Not prioritized"
            else:
                return 'Waiting on builds'

        tests = self.allTests()

        if not tests:
            return "All builds passed."

        for t in tests:
            if t.priority.matches.DependencyFailed:
                return "An underlying dependency failed."

        suitesNotRun = 0
        suitesNotRunAndNotPrioritized = 0
        
        totalTests = 0
        suitesFailed = 0
        totalFailedTestCount = 0

        for t in tests:
            if t.totalRuns == 0:
                suitesNotRun += 1
                if (t.commitData.commit.userPriority == 0 and 
                        t.commitData.commit.calculatedPriority == 0):
                    return "Tests are not prioritized"
            elif t.successes == 0:
                suitesFailed += 1
            else:
                totalTests += t.totalTestCount / t.totalRuns if t.totalRuns != 1 else t.totalTestCount
                totalFailedTestCount += t.totalFailedTestCount / t.totalRuns if t.totalRuns != 1 else t.totalFailedTestCount

        if suitesNotRun:
            return "Waiting on %s / %s test suites to finish" % (
                suitesNotRun, len(tests)
                )
            
        if totalTests == 0:
            if suitesFailed == 0:
                return "%s suites successed" % len(tests)
            else:
                return "%s / %s suites failed" % (suitesFailed, len(tests))

        totalTests = convertToIntIfClose(totalTests)
        totalFailedTestCount = convertToIntIfClose(totalFailedTestCount)

        if suitesFailed:
            return "%s / %s tests failed.  %s / %s suites failed outright (producing no individual test summaries)" % (
                totalFailedTestCount,
                totalTests,
                suitesFailed,
                len(tests)
                )
        else:
            if totalFailedTestCount == 0:
                return "%s tests succeeded over %s suites" % (totalTests, len(tests))

            return "%s / %s tests failed." % (
                totalFailedTestCount,
                totalTests
                )



    def renderMultipleEnvironments(self):
        return "%s builds over %s environments" % (len(self.allBuilds()), len(self.allEnvironments()))

    def categorizeBuild(self, b):
        if b.successes > 0:
            return "OK"
        if b.priority.matches.WaitingToRetry:
            return "PENDING"
        if b.priority.matches.DependencyFailed or b.totalRuns > 0:
            return "BAD"
        return "PENDING"

    def renderSingleEnvironment(self):
        #first, see if all of our builds have completed
        goodBuilds,badBuilds,waitingBuilds = self.categorizeAllBuilds()

        if badBuilds:
            return """<span class="text-danger">%s</span>""" % octicon("x")

        if len(waitingBuilds):
            if (waitingBuilds[0].commitData.commit.userPriority == 0 and 
                    waitingBuilds[0].commitData.commit.calculatedPriority == 0):
                return '<span class="text-muted">%s</span>' % "..."
            return "..."

        tests = self.allTests()

        if not tests:
            return octicon("check")

        totalTests = 0
        totalFailedTestCount = 0

        suitesNotRun = 0
        depFailed = 0
        for t in tests:
            if t.priority.matches.DependencyFailed:
                depFailed += 1
            elif t.totalRuns == 0:
                suitesNotRun += 1
            else:
                totalTests += t.totalTestCount / t.totalRuns
                totalFailedTestCount += t.totalFailedTestCount / t.totalRuns

        if depFailed:
            return '<span class="text-muted">%s</span>' % octicon("x")

        if suitesNotRun:
            if tests[0].commitData.commit.userPriority == 0:
                return '<span class="text-muted">%s</span>' % "..."
            return "..."
            
        if totalTests == 0:
            return '<span class="text-muted">%s</span>' % octicon("check")

        return self.renderFailureCount(totalFailedTestCount, totalTests)

    def renderFailureCount(self, totalFailedTestCount, totalTests, verbose=False):
        if not verbose:
            if totalTests == 0:
                return '<span class="text-muted">%s</span>' % octicon("check")

            if totalFailedTestCount == 0:
                return '%d%s' % (totalTests, '<span class="text-success">%s</span>' % octicon("check"))

        if verbose:
            return '<span class="text-danger">%d</span>%s%d' % (totalFailedTestCount, '<span class="text-muted px-1"> failed out of </span>', totalTests)
        else:
            return '<span class="text-danger">%d</span>%s%d' % (totalFailedTestCount, '<span class="text-muted px-1">/</span>', totalTests)
