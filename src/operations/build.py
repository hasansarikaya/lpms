# Copyright 2009 - 2011 Burak Sezer <purak@hadronproject.org>
# 
# This file is part of lpms
#  
# lpms is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#   
# lpms is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#   
# You should have received a copy of the GNU General Public Licens
# along with lpms.  If not, see <http://www.gnu.org/licenses/>.

import os
import re
import glob
import random
import cPickle as pickle

import lpms

from lpms import out
from lpms import conf
from lpms import utils
from lpms import fetcher
from lpms import internals
from lpms import initpreter
from lpms import shelltools
from lpms import interpreter
from lpms import constants as cst

from lpms.db import dbapi
from lpms.operations import merge

# FIXME: This module is very ugly. I will re-write it.

class Build(internals.InternalFuncs):
    def __init__(self):
        super(Build, self).__init__()
        self.repo_db = dbapi.RepositoryDB()
        self.download_plan = []
        self.extract_plan = []
        self.urls = []
        self.env.__dict__.update({"get": self.get, "cmd_options": [], "options": []})
        self.spec_file = None
        self.config = conf.LPMSConfig()
        if not lpms.getopt("--unset-env-variables"):
            utils.set_environment_variables()
        self.revisioned = False
        self.revision = None

    def set_local_environment_variables(self):
        switches = ["ADD", "REMOVE", "GLOBAL"]
        for item in cst.local_env_variable_files:
            if not os.access(item, os.R_OK):
                continue
            variable_type = item.split("/")[-1].upper()
            with open(item) as data:
                for line in data.readlines():
                    add = []; remove = []; global_flags = []
                    if line.startswith("#"):
                        continue
                    myline = [i.strip() for i in line.split(" ")]
                    target = myline[0]
                    if len(target.split("/")) == 2:
                        if target != self.env.category+"/"+self.env.name:
                            continue
                    elif len(target.split("/")) == 1:
                        if target != self.env.category:
                            if len(target.split("-")) == 1:
                                out.warn("warning: invalid line found in %s:" % item)
                                out.red("   "+line)
                            continue
                    else:
                        if len(target.split("-")) == 1:
                            out.warn("warning: invalid line found in %s:" % item)
                            out.red("   "+line)
                            continue

                    if variable_type == "ENV":
                        if myline[1] == "UNSET":
                            variable = myline[2]
                            if variable in os.environ:
                                del os.environ[variable]
                        else:
                            try:
                                variable, value = myline[1:]
                            except ValueError:
                                out.warn("warning: invalid line found in %s:" % item)
                                out.red("   "+line)
                            else:
                                os.environ[variable] = value

                    for switch in switches:
                        if not switch in myline[1:]:
                            continue
                        switch_index = myline.index(switch)
                        for word in myline[switch_index+1:]:
                            if word in switches: 
                                break
                            if switch == "GLOBAL":
                                global_flags.append(word)
                            if switch == "ADD":
                                add.append(word)
                            elif switch == "REMOVE":
                                remove.append(word)
                    
                    if global_flags:
                        if variable_type in os.environ:
                            del os.environ[variable_type]
                            os.environ[variable_type] = " ".join(global_flags)
                    else:
                        if add:
                            if variable_type in os.environ:
                                current = os.environ[variable_type]
                                current += " "+" ".join(add)
                                os.environ[variable_type] = current
                            else:
                                out.warn("%s not defined in your environment" % variable_type)
                        if remove:
                            if variable_type in os.environ:
                                current = os.environ[variable_type]
                                new = [atom for atom in current.split(" ") if not atom in remove]
                                os.environ[variable_type] = " ".join(new)
                            else:
                                out.warn("%s not defined in your environment" % variable_type)

    def options_info(self):
        # FIXME: This is no good.
        if self.env.options is not None:
            self.env.options = self.env.options.split(" ")
        else:
            self.env.options = []
        return [o for o in self.env.options if utils.opt(o, self.env.cmd_options, 
            self.env.default_options)]

    def check_cache(self, url):
        return os.path.isfile(
                os.path.join(self.config.src_cache,
                os.path.basename(url)))

    def prepare_download_plan(self, applied):
        for url in self.urls:
            if not isinstance(url, tuple):
                self.extract_plan.append(url)
                if self.check_cache(url):
                    continue
                self.download_plan.append(url)
            else:
                option, url = url
                if self.check_cache(url):
                    continue
                if option in applied: 
                    self.download_plan.append(url)
                    self.extract_plan.append(url)
        setattr(self.env, "extract_plan", self.extract_plan)

    def prepare_environment(self):
        if self.env.sandbox is None:
            if not self.config.sandbox and lpms.getopt("--enable-sandbox"):
                self.env.__setattr__("sandbox", True)
            elif self.config.sandbox and not lpms.getopt("--ignore-sandbox"):
                self.env.__setattr__("sandbox", True)

        self.env.build_dir = os.path.join(self.config.build_dir, 
            self.env.category, self.env.fullname, "source", self.env.srcdir)
        self.env.install_dir = os.path.join(self.config.build_dir, 
            self.env.category, self.env.fullname, "install")
        
        try:
            if not lpms.getopt("--resume-build") and len(os.listdir(self.env.install_dir)) != 0:
                shelltools.remove_dir(self.env.install_dir)
        except OSError:
            pass

        for i in ('build_dir', 'install_dir'):
            if not os.path.isdir(getattr(self.env, i)):
                os.makedirs(getattr(self.env, i))

    def parse_url_tag(self):
        def set_shortening(data, opt=False):
            for short in ('$url_prefix', '$src_url', '$slot', '$my_slot', '$name', '$version', \
                    '$fullname', '$my_fullname', '$my_name', '$my_version'):
                try:
                    interphase = re.search(r'-r[0-9][0-9]', self.env.__dict__[short[1:]])
                    if not interphase:
                        interphase = re.search(r'-r[0-9]', self.env.__dict__[short[1:]])
                        if not interphase:
                            data = data.replace(short, self.env.__dict__[short[1:]])
                        else:
                            if short == "$version": self.revisioned = True; self.revision = interphase.group()
                            result = "".join(self.env.__dict__[short[1:]].split(interphase.group()))
                            if not short in ("$name", "$my_slot", "$slot"): setattr(self.env, "raw_"+short[1:], result)
                            data = data.replace(short, result)
                    else:
                        if short == "$version": self.revisioned = True; self.revision = interphase.group()
                        result = "".join(self.env.__dict__[short[1:]].split(interphase.group()))
                        if not short in ("$name", "$my_slot", "$slot"): setattr(self.env, "raw_"+short[1:], result)
                        data = data.replace(short, result)
                except KeyError: pass
            if opt:
                self.urls.append((opt, data))
            else:
                self.urls.append(data)

        for url in self.env.src_url.split(" "):
            result = url.split("(")
            if len(result) == 1:
                set_shortening(url)
            elif len(result) == 2:
                option, url = result
                url = url.replace(")", "")
                set_shortening(url, opt=option)

    def compile_script(self):
        if not os.path.isfile(self.env.spec_file):
            lpms.catch_error("%s not found!" % self.env.spec_file)
        if not self.import_script(self.env.spec_file):
            out.error("an error occured while processing the spec: %s" \
                    % out.color(self.env.spec_file, "red"))
            out.error("please report the above error messages to the package maintainer.")
            lpms.terminate()

def main(raw_data, instruct):
    operation_plan, operation_data, modified_by_package = raw_data
    # resume previous operation_plan
    # if skip_first returns True, skip first package
    resume_file =  os.path.join("/", cst.resume_file) if instruct['real_root'] \
            is None else os.path.join(instruct['real_root'], cst.resume_file)
    if not os.path.isdir(os.path.dirname(resume_file)):
        os.makedirs(os.path.dirname(resume_file))
    if instruct["resume"]:
        if os.path.exists(resume_file):
            with open(resume_file, "rb") as _data:
                stored_data = pickle.load(_data)
                operation_plan, operation_data, modified_by_package \
                        = stored_data['raw_data']
                instruct['real_root'] = stored_data['real_root']
                if instruct["skip-first"]:
                    operation_plan = operation_plan[1:]

                if not operation_plan:
                    out.error("resume failed! package query not found.")
                    lpms.terminate()
        else:
            out.error("%s not found" % resume_file)
            lpms.terminate()

    count = len(operation_plan); i = 1
    if instruct["pretend"] or instruct["ask"]:
        out.write("\n")
        out.normal("these packages will be merged, respectively:\n")
        for atom in operation_plan:
            data, valid_options = operation_data[atom]
            repo, category, name, version  = atom
            options = dbapi.RepositoryDB().get_options(repo, category, name)[version]
            show_plan(repo, category, name, version, valid_options, options, modified_by_package)
            if data['conflict']:
                for conflict in data['conflict']:
                    # the last item is options of the package
                    crepo, ccategory, cname, cversion = conflict
                    out.write(" %s %s/%s/%s-%s\n" % (out.color("conflict:", "brightred"),
                        out.color(crepo, "green"),
                        out.color(ccategory, "green"),
                        out.color(cname, "green"),
                        out.color(cversion, "green")))

        if instruct["pretend"]:
            lpms.terminate()

        utils.xterm_title("lpms: confirmation request")
        out.write("\nTotal %s package will be merged.\n\n" % out.color(str(count), "green"))
        if not utils.confirm("do you want to continue?"):
            out.write("quitting...\n")
            utils.xterm_title_reset()
            lpms.terminate()
            
    # clean source code extraction directory if it is wanted
    if lpms.getopt("--clean-tmp"):
        clean_tmp_exceptions = ("resume")
        for item in shelltools.listdir(cst.extract_dir):
            if item in clean_tmp_exceptions: continue
            path = os.path.join(cst.extract_dir, item)
            if path in clean_tmp_exceptions: continue
            if os.path.isdir(path):
                shelltools.remove_dir(path)
            else:
                shelltools.remove_file(path)

    # resume feature
    # create a resume list. write package data(repo, category, name, version) to 
    # /var/tmp/lpms/resume file.
    if not instruct["resume"] or instruct["skip-first"]:
        if os.path.exists(resume_file):
            shelltools.remove_file(resume_file)
        with open(resume_file, "wb") as _data:
            pickle.dump({'raw_data': raw_data, 'real_root': instruct['real_root']}, _data)

    if not os.path.ismount("/proc"):
        out.warn("/proc is not mounted. You have been warned.")
    if not os.path.ismount("/dev"):
        out.warn("/dev is not mounted. You have been warned.")

    for plan in operation_plan:
        opr = Build()
        # if conflict list is not empty,
        # remove the packages in the list.
        # To do this, run spec interpreter once again. 
        if operation_data[plan][0]["conflict"]:
            # prepare the environment variables
            conflict_instruct = {"ask": False, "real_root": instruct["real_root"],
                    "count": len(operation_data[plan][0]["conflict"])}
            i = 0
            for conflict in operation_data[plan][0]["conflict"]:
                i += 1;
                conflict_instruct['i'] = i
                # run the interpreter with remove parameter
                # operation_order => ["remove"]
                if not initpreter.InitializeInterpreter(conflict, conflict_instruct, 
                        ['remove'], remove=True).initialize():
                    repo, category, name, version = conflict
                    out.error("an error occured during remove operation: %s/%s/%s-%s" % (repo, category, name, version))
                    lpms.terminate()
            out.write("\n")

        setattr(opr.env, 'todb', operation_data[plan][0])
        setattr(opr.env, 'valid_opts', operation_data[plan][1])

        operation_data[plan]
        keys = {'repo':0, 'category':1, 'pkgname':2, 'version':3}
        for key in keys:
            setattr(opr.env, key, plan[keys[key]])

        interphase = re.search(r'-r[0-9][0-9]', opr.env.version)
        if not interphase:
            interphase = re.search(r'-r[0-9]', opr.env.version)
        try:
            opr.env.raw_version = opr.env.version.replace(interphase.group(), "")
        except AttributeError:
            opr.env.raw_version = opr.env.version
        
        # FIXME:
        opr.env.name = opr.env.pkgname
        opr.env.fullname = opr.env.pkgname+"-"+opr.env.version
        opr.env.spec_file = os.path.join(cst.repos, opr.env.repo, 
                opr.env.category, opr.env.pkgname, opr.env.pkgname)+"-"+opr.env.version+cst.spec_suffix
        opr.env.__dict__.update(instruct)
        opr.env.default_options = opr.config.options.split(" ")

        # set local environment variables
        if not lpms.getopt("--unset-env-variables"):
            opr.set_local_environment_variables()

        opr.compile_script()
        
        metadata = utils.metadata_parser(opr.env.metadata)
        for attr in ('options', 'summary', 'license', 'homepage', 'slot', 'arch'):
            try:
                setattr(opr.env, attr, metadata[attr])
            except KeyError:
                # slot?
                if attr == "slot":
                    setattr(opr.env, attr, "0")
                # arch
                elif attr == "arch":
                    setattr(opr.env, attr, None)

        setattr(opr.env, "i", i)
        setattr(opr.env, "count", count)
        setattr(opr.env, "filesdir", os.path.join(cst.repos, opr.env.repo, 
            opr.env.category, opr.env.pkgname, cst.files_dir))
        setattr(opr.env, "src_cache", cst.src_cache)

        # FIXME: This is no good!
        ####################################
        if opr.env.options is None:
            opr.env.options = []

        if opr.env.valid_opts is None:
            opr.env.valid_opts = []
        ####################################

        if "src_url" in metadata:
            opr.env.src_url = metadata["src_url"]
        else:
            if not "src_url" in opr.env.__dict__.keys():
                opr.env.src_url = None

        if not "srcdir" in opr.env.__dict__:
            setattr(opr.env, "srcdir", opr.env.pkgname+"-"+opr.env.raw_version)
        opr.prepare_environment()

        # start logging
        # we want to save starting time of the build operation
        lpms.logger.info("starting build (%s/%s) %s/%s/%s-%s" % (i, count, opr.env.repo, 
            opr.env.category, opr.env.name, opr.env.version))

        if random.randrange(0, 1000001) in range(0, 1001):
            data = """The Zen of Python, by Tim Peters
            Beautiful is better than ugly.
            Explicit is better than implicit.
            Simple is better than complex.
            Complex is better than complicated.
            Flat is better than nested.
            Sparse is better than dense.
            Readability counts.
            Special cases aren't special enough to break the rules.
            Although practicality beats purity.
            Errors should never pass silently.
            Unless explicitly silenced.
            In the face of ambiguity, refuse the temptation to guess.
            There should be one-- and preferably only one --obvious way to do it.
            Although that way may not be obvious at first unless you're Dutch.
            Now is better than never.
            Although never is often better than *right* now.
            If the implementation is hard to explain, it's a bad idea.
            If the implementation is easy to explain, it may be a good idea.
            Namespaces are one honking great idea -- let's do more of those!"""
            data = data.split("\n")
            out.normal(data[0]+": "+data[random.randrange(2, 19)].strip())

        out.normal("(%s/%s) building %s/%s from %s" % (i, count,
            out.color(opr.env.category, "green"),
            out.color(opr.env.pkgname+"-"+opr.env.version, "green"), opr.env.repo)); i += 1

        out.notify("you are using %s userland and %s kernel" % (opr.config.userland, opr.config.kernel))

        if opr.env.sandbox:
            lpms.logger.info("sandbox enabled build")
            out.notify("sandbox is enabled")
        else:
            lpms.logger.warning("sandbox disabled build")
            out.warn_notify("sandbox is disabled")

        # fetch packages which are in download_plan list
        if opr.env.src_url is not None:
            # preprocess url tags such as $name, $version and etc
            opr.parse_url_tag()
            # if the package is revisioned, override build_dir and install_dir. remove revision number from these variables.
            if opr.revisioned:
                for variable in ("build_dir", "install_dir"):
                    new_variable = "".join(os.path.basename(getattr(opr.env, variable)).split(opr.revision))
                    setattr(opr.env, variable, os.path.join(os.path.dirname(getattr(opr.env, \
                            variable)), new_variable))

            utils.xterm_title("lpms: downloading %s/%s/%s-%s" % (opr.env.repo, opr.env.category,
                opr.env.name, opr.env.version))
            
            opr.prepare_download_plan(opr.env.valid_opts)
            
            if not fetcher.URLFetcher().run(opr.download_plan):
                lpms.catch_error("\nplease check the spec")

        if opr.env.valid_opts is not None and len(opr.env.valid_opts) != 0:
            out.notify("applied options: %s" % 
                    " ".join(opr.env.valid_opts))

        # set writable sandbox paths for build operation
        utils.set_sandbox_paths()
        # remove previous sandbox log if it is exist.
        if os.path.exists(cst.sandbox_log):
            shelltools.remove_file(cst.sandbox_log)
        os.chdir(opr.env.build_dir)
        
        if not interpreter.run(opr.env.spec_file, opr.env):
            lpms.terminate("thank you for flying with lpms.")
            
        lpms.logger.info("finished %s/%s/%s-%s" % (opr.env.repo, opr.env.category, 
            opr.env.name, opr.env.version))

        utils.xterm_title("lpms: %s/%s finished" % (opr.env.category, opr.env.pkgname))

        out.notify("cleaning build directory...\n")
        shelltools.remove_dir(os.path.dirname(opr.env.install_dir))
        catdir = os.path.dirname(os.path.dirname(opr.env.install_dir))
        if not os.listdir(catdir):
            shelltools.remove_dir(catdir)


        # resume feature
        # delete package data, if it is installed successfully
        with open(resume_file, "rb") as _data:
            stored_data = pickle.load(_data)
            resume_plan, resume_data, modified_by_package = stored_data['raw_data']
            resume_real_root = stored_data['real_root']
        new_resume_plan = []

        for pkg in resume_plan:
            if pkg != (opr.env.repo, opr.env.category, 
                    opr.env.name, opr.env.version):
                new_resume_plan.append(pkg)

        shelltools.remove_file(resume_file)
        with open(resume_file, "wb") as _data:
            pickle.dump({'raw_data': (new_resume_plan, resume_data, modified_by_package), 'real_root': resume_real_root}, _data)

        opr.env.__dict__.clear()
        utils.xterm_title_reset()

def show_plan(repo, category, name, version, valid_options, options, modified_by_package):
    result = []; status = [' ', '  ']; oldver= ""

    instdb = dbapi.InstallDB()
    repodb = dbapi.RepositoryDB()

    pkgdata = instdb.find_pkg(name, pkg_category=category)

    if pkgdata:
        repovers = repodb.get_version(name, pkg_category = category)
        slot = instdb.get_slot(category, name, version)
        repo_slot = repodb.get_slot(category, name, version)

        if slot is None and not repo_slot in pkgdata[-1]:
            status[-1] = out.color("NS", "brightgreen")
        else:
            if slot is None:
                slot = repo_slot
            if isinstance(pkgdata, list):
                for item in pkgdata:
                    if slot in item[-1]:
                        pkgdata = item
                        break
            if version in repovers[repo_slot]:
                instver = pkgdata[-1][slot][0]
                cmpres = utils.vercmp(version, instver)
                if cmpres == 1:
                    status[0] = out.color("U", "brightgreen")
                    oldver = "["+out.color(instver, "brightgreen")+"]"
                elif cmpres == 0:
                    status[0] = out.color("R", "brightyellow")
                elif cmpres == -1:
                    status[0] = out.color("D", "brightred")
                    oldver = "["+out.color(instver, "brightred")+"]"
    else:
        status[0] = out.color("N", "brightgreen")


    out.write("[%s] %s/%s/%s-%s %s " % (" ".join(status), out.color(repo, "green"), out.color(category, "green"), 
        out.color(name, "green"), out.color(version, "green"), oldver))
    
    if options:
        try:
            irepo = instdb.get_repo(category, name, version)
            instopts = instdb.get_options(irepo, category, name)[version].split(" ")
        except (KeyError, TypeError):
            instopts = None

        for o in options.split(" "):
            if valid_options and o in valid_options:
                if instopts and not o in instopts:
                    result.insert(0, out.color(o+"*", "brightgreen"))
                    continue
                result.insert(0, out.color(o, "red"))
            else:
                if instopts and o in instopts:
                    result.insert(len(result)+1, "-"+out.color(o+"*", "brightyellow"))
                    continue
                result.insert(len(result)+1, "-"+o)

        out.write("("+" ".join(result)+")")
        if (repo, category, name, version) in modified_by_package:
            for item in modified_by_package[(repo, category, name, version)]:
                interfere_repo, interfere_category, interfere_name, \
                        interfere_version, wanted_options = item
                out.red("\n       >> the package is modified by %s/%s/%s-%s (%s)" % (interfere_repo, interfere_category, \
                        interfere_name, interfere_version, ",".join(wanted_options)))
    out.write("\n")

