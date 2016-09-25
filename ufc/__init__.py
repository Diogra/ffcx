__author__ = "Johan Hake (hake.dev@gmail.com)"
__copyright__ = "Copyright (C) 2010-2015 Johan Hake"
__date__ = "2010-08-19 -- 2015-02-26"
__license__  = "Released to the public domain"

from os.path import dirname, abspath

def get_include_path():
    return dirname(abspath(__file__))
