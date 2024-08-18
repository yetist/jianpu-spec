#! /usr/bin/env python
# -*- encoding:utf-8 -*-
# FileName: tt.py

"This file is part of ____"

__author__   = "yetist"
__copyright__= "Copyright (C) 2017 yetist <yetist@yetibook>"
__license__  = """
This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program; if not, write to the Free Software Foundation, Inc.,
51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
"""

import os, sys
import urllib.request
import urllib.parse
import argparse
import json

DEFAULT_PAGE_CONFIG = {'biaoti_font': 'Microsoft YaHei',
        'biaoti_size': '36',
        'body_margin_top': '40',
        'fubiaoti_size': '20',
        'geci_font': 'Microsoft YaHei',
        'geci_size': '18',
        'height_cici': '10',
        'height_ciqu': '40',
        'height_quci': '13',
        'height_shengbu': '0',
        'lianyinxian_type': '0',
        'margin_bottom': '80',
        'margin_left': '80',
        'margin_right': '80',
        'margin_top': '80',
        'page': 'A4',
        'shuzi_font': 'b'}


def gen_page_config(path):
    cfg = DEFAULT_PAGE_CONFIG
    if os.path.exists(path):
        fd = open(path)
        data = json.load(fd)
        fd.close()
        for i in data.keys():
            cfg[i] = data[i]
    return json.dumps(cfg)

config = {
        "format":"svg",
        "outdir":".",
        "config":"config.js",
        "number":"0",
        "power":""
        }

def gen_custom_code(args):
    code = ""
    tmpl = """<defs>
<g id="%(id)s" data-type="text">
<text x="0" y="0" fill="#101010" font-family="%(font)s" font-size="%(size)d" style="font-weight:normal;" dy="14.08">%(text)s</text>
</g></defs>
<use id="%(id)s" x="%(x)d" y="%(y)d" xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="#%(id)s">
</use>"""
    data = {
            "id":"",
            "font":"Microsoft YaHei",
            "size":"24",
            "text":"",
            "x":0,
            "y":0
            }

    if args.num is not None and args.num > 0:
        data['id'] = 'custom_music_number'
        data['size'] = 40
        data['text'] = str(args.num)
        if args.num % 2 == 0:
            data['x'] = 80
            data['y'] = 130
        else:
            data['x'] = 880
            data['y'] = 130
        code += tmpl % data
    if args.powerdby is not None:
        data['id'] = 'custom_powerdby'
        data['size'] = 24
        data['text'] = args.powerdby
        data['x'] = 10
        data['y'] = 10
        code += tmpl % data
    return code

def gendata(args):
    params = {}
    data = open(args.file).read()
    code = data .replace('\n', '&hh&')
    params['code'] = code
    params['customCode'] =  gen_custom_code(args)
    params['pageConfig'] = gen_page_config(args.config)
    params['pageNum'] = "-1"

    data = urllib.parse.urlencode(params)
    data = data.encode('utf-8')

    return data

def jianpu(args):
    req = urllib.request.Request('http://zhipu.lezhi99.com/Zhipu-draw')
    req.add_header('Referer', 'http://zhipu.lezhi99.com/Zhipu-index.html')
    req.add_header('User-Agent', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/63.0.3239.84 Safari/537.36')
    req.add_header("Content-Type","application/x-www-form-urlencoded;charset=utf-8")

    data = gendata(args)
    if data:
        r = urllib.request.urlopen(req, data)
    else:
        r = urllib.request.urlopen(req)
    svgd = r.read().decode('utf-8')
    gepus = svgd.split('[fenye]')
    music = []
    for g in gepus:
        if len(g) > 0:
            music.append(g.replace("\r", ""))
    pages = len(music)
    paths = os.path.split(args.file)
    names = os.path.splitext(paths[1])

    for i in range(pages):
        if i == 0 and len(music) == 1:
            newpath = os.path.join(args.outdir, names[0]+".svg")
            outpath = os.path.join(args.outdir, names[0]+"." + args.format)
        else:
            newpath = os.path.join(args.outdir, "%s-%d.svg" % (names[0], i))
            outpath = os.path.join(args.outdir, "%s-%d" + "." + args.format % (names[0], i))
        fw = open(newpath, "w+")
        fw.write(music[i].replace('&', '+'))
        fw.close()
        if args.format != "svg":
            os.system("rsvg-convert -f %s -o %s %s" % (args.format, outpath, newpath))

if __name__=="__main__":
    parser = argparse.ArgumentParser(description='Jianpu generate.')
    parser.add_argument('-f', '--format', metavar='FORMAT', dest='format', choices=['pdf', 'png', 'svg', 'jpg'],
            default='svg',
            help='output format(svg, png, jpg, pdf), default is "svg"')
    parser.add_argument('-d', '--outdir',  metavar='DIR', dest='outdir', default='.',
            help='output directory, default is "."')
    parser.add_argument('-c', '--config', metavar='CONFIG', dest='config',  default='config.js',
            help='config file, default is "config.js"')
    parser.add_argument('-n', '--num', metavar='NUM', dest='num', type=int,
            help='music number.')
    parser.add_argument('-p', '--poweredby', metavar='STR', dest='powerdby',
            help='powerd by string.')
    parser.add_argument('file', metavar='FILE', type=str,
                   help='jps file')
    args = parser.parse_args()
    jianpu(args)

