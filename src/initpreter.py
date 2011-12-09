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
# You should have received a copy of the GNU General Public License
# along with lpms.  If not, see <http://www.gnu.org/licenses/>.

import os

from lpms import internals
from lpms import interpreter

from lpms import constants as cst

# initialize interpreter
#
# this module provies an interface to run lpms's package specification 
# interpreter for any purpose except that build operation.

class InitializeInterpreter(internals.InternalFuncs):
    '''Base class for initialize the lpms spec interpreter
    It can be used for any purpose'''
    def __init__(self, script, instruct, operations, remove=False):
        super(InitializeInterpreter, self).__init__()
        self.script = script
        self.remove = remove
        self.env.__dict__.update(instruct)
        self.operations = operations
        self.env.__dict__["get"] = self.get

    def initialize(self):
        '''Registers some basic environmet variables and runs the interpreter'''
        repo, category, name, version = self.script

        for key, data in {"repo": repo, "name": name, "version": version, "category": \
                category, "pkgname": name, "fullname": name+"-"+version}.items():
            setattr(self.env, key, data)

        spec_file = os.path.join(cst.repos, repo, category, \
                name, name)+"-"+version+cst.spec_suffix

        # compile the script
        if os.access(spec_file, os.F_OK):
            self.import_script(spec_file)

        # remove irrelevant functions for environment.
        # because, the following functions must be run by Build class
        # that is defined in operations/build.py
        for func in ('prepare', 'install', 'build', 'configure'):
            if func in self.env.__dict__:
                delattr(self.env, func)

        return interpreter.run(spec_file, self.env, self.operations, self.remove)

