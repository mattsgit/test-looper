import test_looper.server.rendering.Context as Context
import test_looper.server.rendering.TestGridRenderer as TestGridRenderer
import test_looper.server.rendering.ComboContexts as ComboContexts
import test_looper.server.HtmlGeneration as HtmlGeneration

class ReposContext(Context.Context):
    def __init__(self, renderer, options):
        Context.Context.__init__(self, renderer, options)
        self.options = options

    def renderLink(self):
        return HtmlGeneration.link("Repos", self.urlString())

    def consumePath(self, path):
        if path:
            path, remainder = self.popToDash(path)

            repo = self.database.Repo.lookupAny(name = "/".join(path))

            if repo:
                return self.renderer.contextFor(repo, self.options), remainder

            return None, path

        return None, path

    def urlBase(self):
        return "repos"

    def primaryObject(self):
        return "repos"

    def renderPageBody(self):
        headers, grid = self.grid()

        if not headers:
            res = card("No repos found")
        else:
            res = HtmlGeneration.grid(headers+grid, header_rows=len(headers))
        
        return res

    def grid(self):
        repos = self.database.Repo.lookupAll(isActive=True)

        repos = [r for r in repos if self.renderer.wantsToShowRepo(r)]
            
        if not repos:
            return [], []

        repos = sorted(
            repos, 
            key=lambda repo:
                (repo.commitsWithTests == 0, repo.name)
            )

        best_branch = {}
        test_rows = {}
        best_commit = {}
        best_commit_name = {}

        for r in repos:
            best_branch[r] = self.primaryBranchForRepo(r)

            best_commit[r],best_commit_name[r] = self.renderer.bestCommitForBranch(best_branch[r])

            test_rows[r] = self.renderer.allTestsForCommit(best_commit[r]) if best_commit[r] and best_commit[r].userPriority else []

        gridRenderer = TestGridRenderer.TestGridRenderer(
            repos, 
            lambda r: test_rows.get(r, []),
            lambda group: "",
            lambda group, row: 
                self.contextFor(best_branch[row]).urlString()
                if best_branch[row] else "",
            lambda test: ""
            )

        grid_headers = [gridRenderer.headers()]

        for additionalHeader in reversed(["REPO NAME", "TOP TESTED COMMIT"]):
            grid_headers = [[""] + g for g in grid_headers]
            grid_headers[-1][0] = additionalHeader

        grid = []
        last_repo = None
        for repo in repos:
            if last_repo and last_repo.commitsWithTests and not repo.commitsWithTests:
                grid.append([""])
            last_repo = repo

            branches = self.database.Branch.lookupAll(repo=repo)

            if best_commit[repo] and best_commit[repo].userPriority:
                testRow = gridRenderer.gridRow(repo)
            else:
                testRow = [""] * len(gridRenderer.groups)

            grid.append([
                self.contextFor(repo).renderLink(),
                self.contextFor(best_commit[repo]).renderLink(includeRepo=False) if best_commit[repo] else ""
                ] + testRow)

        return grid_headers, grid

    def primaryBranchForRepo(self, repo):
        branches = [b for b in self.database.Branch.lookupAll(repo=repo)
            if b.branchname.endswith("master-looper")]

        if len(branches) == 1:
            return branches[0]

        for branchname in ["master", "svn-master"]:
            master = self.database.Branch.lookupAny(reponame_and_branchname=(repo.name, branchname))
            if master:
                return master

    def childContexts(self, currentChild):
        children = []

        for r in sorted(self.database.Repo.lookupAll(isActive=True),key=lambda r:r.name):
            if self.renderer.wantsToShowRepo(r):
                if r.commitsWithTests or r == currentChild:
                    children.append(r)

        return [self.contextFor(x) for x in children]

    def parentContext(self):
        return self.contextFor("root")
        
    def renderMenuItemText(self, isHeader):
        return "Repos"




