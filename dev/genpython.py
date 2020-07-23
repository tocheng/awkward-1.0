# BSD 3-Clause License; see https://github.com/scikit-hep/awkward-1.0/blob/master/LICENSE

import argparse
import os
from collections import OrderedDict

import black
import pycparser

from parser_utils import check_fail_func, gettokens, parseheader, preprocess
from testkernels import gentests

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))


class FuncBody(object):
    def __init__(self, ast):
        self.ast = ast
        self.code = ""
        self.traverse(self.ast.block_items, 4)

    def traverse(self, item, indent, called=False):
        if item.__class__.__name__ == "list":
            for node in item:
                self.traverse(node, indent)
        elif item.__class__.__name__ == "Return":
            if (
                item.expr.__class__.__name__ == "FuncCall"
                and item.expr.name.name == "failure"
            ):
                stmt = " " * indent + "raise ValueError({0})".format(
                    item.expr.args.exprs[0].value
                )
            elif (
                item.expr.__class__.__name__ == "FuncCall"
                and item.expr.name.name == "success"
                and item.expr.args is None
            ):
                stmt = " " * indent + "return"
            else:
                stmt = " " * indent + "return {0}".format(
                    self.traverse(item.expr, 0, called=True)
                )
            if called:
                return stmt
            else:
                self.code += stmt + "\n"
        elif item.__class__.__name__ == "Constant":
            stmt = " " * indent + "{0}".format(item.value)
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "Decl":
            if item.init is not None:
                stmt = " " * indent + "{0} = {1}".format(
                    item.name, self.traverse(item.init, 0, called=True)
                )
                if not called:
                    stmt = stmt + "\n"
            elif item.type.__class__.__name__ == "PtrDecl":
                stmt = " " * indent + "{0} = []".format(item.name)
                if not called:
                    stmt = stmt + "\n"
            else:
                stmt = ""
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "Assignment":
            if (
                item.rvalue.__class__.__name__ == "ArrayRef"
                and item.rvalue.subscript.__class__.__name__ == "BinaryOp"
                and item.rvalue.subscript.left.__class__.__name__ == "UnaryOp"
                and item.rvalue.subscript.left.op == "++"
            ):
                stmt = " " * indent + "{0} += 1; {1} = {2}[{0} {3} {4}]".format(
                    self.traverse(item.rvalue.subscript.left.expr, 0, called=True),
                    self.traverse(item.lvalue, 0, called=True),
                    self.traverse(item.rvalue.name, 0, called=True),
                    item.rvalue.subscript.op,
                    self.traverse(item.rvalue.subscript.right, 0, called=True),
                )
            else:
                stmt = " " * indent + "{0} {1} {2}".format(
                    self.traverse(item.lvalue, 0, called=True),
                    item.op,
                    self.traverse(item.rvalue, 0, called=True),
                )
            if called:
                return stmt
            else:
                self.code += stmt + "\n"
        elif item.__class__.__name__ == "FuncCall":
            if item.args is not None:
                if item.name.name == "memcpy":
                    return " " * indent + "{0}[{1}:{1}+{2}] = {3}[{4}:{4}+{2}]".format(
                        item.args.exprs[0].expr.name.name,
                        self.traverse(
                            item.args.exprs[0].expr.subscript, 0, called=True
                        ),
                        item.args.exprs[2].name,
                        item.args.exprs[1].expr.name.name,
                        self.traverse(
                            item.args.exprs[1].expr.subscript, 0, called=True
                        ),
                    )
                return " " * indent + "{0}({1})".format(
                    item.name.name, self.traverse(item.args, 0, called=True)
                )
            else:
                return " " * indent + "{0}()".format(item.name.name)
        elif item.__class__.__name__ == "ExprList":
            stmt = " " * indent
            for i in range(len(item.exprs)):
                if i == 0:
                    stmt += "{0}".format(self.traverse(item.exprs[i], 0, called=True))
                else:
                    stmt += ", {0}".format(self.traverse(item.exprs[i], 0, called=True))
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "BinaryOp":
            if item.op == "&&":
                operator = "and"
            elif item.op == "||":
                operator = "or"
            else:
                operator = item.op
            binaryopl = "{0}".format(self.traverse(item.left, 0, called=True))
            binaryopr = "{0}".format(self.traverse(item.right, 0, called=True))
            if called and item.left.__class__.__name__ == "BinaryOp":
                binaryopl = "(" + binaryopl + ")"
            if called and item.right.__class__.__name__ == "BinaryOp":
                binaryopr = "(" + binaryopr + ")"
            stmt = " " * indent + "{0} {1} {2}".format(binaryopl, operator, binaryopr)
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "If":
            stmt = " " * indent + "if {0}:\n".format(
                self.traverse(item.cond, 0, called=True)
            )
            stmt += "{0}\n".format(self.traverse(item.iftrue, indent + 4, called=True))
            if item.iffalse is not None:
                stmt += " " * indent + "else:\n"
                stmt += "{0}\n".format(
                    self.traverse(item.iffalse, indent + 4, called=True)
                )
            if called:
                return stmt[:-1]
            else:
                self.code += stmt
        elif item.__class__.__name__ == "For":
            if (
                (item.init is not None)
                and (item.next is not None)
                and (item.cond is not None)
                and (len(item.init.decls) == 1)
                and (
                    item.init.decls[0].init.__class__.__name__ == "Constant"
                    or item.init.decls[0].init.__class__.__name__ == "ID"
                )
                and (item.next.op == "p++")
                and (item.cond.op == "<")
                and (item.cond.left.name == item.init.decls[0].name)
            ):
                if item.init.decls[0].init.__class__.__name__ == "Constant":
                    if item.init.decls[0].init.value == "0":
                        stmt = " " * indent + "for {0} in range(int({1})):\n".format(
                            item.init.decls[0].name,
                            self.traverse(item.cond.right, 0, called=True),
                        )
                    else:
                        stmt = (
                            " " * indent
                            + "for {0} in range(int({1}), {2}):\n".format(
                                item.init.decls[0].name,
                                item.init.decls[0].init.value,
                                self.traverse(item.cond.right, 0, called=True),
                            )
                        )
                else:
                    stmt = " " * indent + "for {0} in range(int({1}), {2}):\n".format(
                        item.init.decls[0].name,
                        item.init.decls[0].init.name,
                        self.traverse(item.cond.right, 0, called=True),
                    )
                for i in range(len(item.stmt.block_items)):
                    stmt += (
                        self.traverse(item.stmt.block_items[i], indent + 4, called=True)
                        + "\n"
                    )
            else:
                if item.init is not None:
                    stmt = "{0}\n".format(self.traverse(item.init, indent, called=True))
                else:
                    stmt = ""
                stmt += " " * indent + "while {0}:\n".format(
                    self.traverse(item.cond, 0, called=True)
                )
                for i in range(len(item.stmt.block_items)):
                    stmt += (
                        self.traverse(item.stmt.block_items[i], indent + 4, called=True)
                        + "\n"
                    )
                stmt += " " * (indent + 4) + "{0}\n".format(
                    self.traverse(item.next, 0, called=True)
                )
            if called:
                return stmt[:-1]
            else:
                self.code += stmt
        elif item.__class__.__name__ == "UnaryOp":
            if item.op[1:] == "++" or item.op == "++":
                stmt = " " * indent + "{0} = {0} + 1".format(
                    self.traverse(item.expr, 0, called=True)
                )
            elif item.op[1:] == "--":
                stmt = " " * indent + "{0} = {0} - 1".format(
                    self.traverse(item.expr, 0, called=True)
                )
            elif item.op == "*":
                stmt = " " * indent + "{0}[0]".format(
                    self.traverse(item.expr, 0, called=True)
                )
            elif item.op == "-":
                stmt = " " * indent + "-{0}".format(
                    self.traverse(item.expr, 0, called=True)
                )
            elif item.op == "!":
                stmt = " " * indent + "not ({0})".format(
                    self.traverse(item.expr, 0, called=True)
                )
            elif item.op == "&":
                stmt = " " * indent + "{0}".format(
                    self.traverse(item.expr, 0, called=True)
                )
            else:
                raise NotImplementedError(
                    "Unhandled Unary Operator case. Please inform the developers about the error"
                )
            if called:
                return stmt
            else:
                self.code += stmt + "\n"
        elif item.__class__.__name__ == "DeclList":
            stmt = " " * indent
            for i in range(len(item.decls)):
                if i == 0:
                    stmt += "{0}".format(self.traverse(item.decls[i], 0, called=True))
                else:
                    stmt += ", {0}".format(self.traverse(item.decls[i], 0, called=True))
            if called:
                return stmt
            else:
                self.code += stmt + "\n"
        elif item.__class__.__name__ == "ArrayRef":
            if (
                item.subscript.__class__.__name__ == "UnaryOp"
                and item.subscript.op[1:] == "++"
            ):
                stmt = (
                    " " * indent
                    + "{0};".format(self.traverse(item.subscript, 0, called=True))
                    + " {0}[{1}]".format(
                        self.traverse(item.name, 0, called=True),
                        self.traverse(item.subscript.expr, 0, called=True),
                    )
                )
            else:
                stmt = " " * indent + "{0}[{1}]".format(
                    self.traverse(item.name, 0, called=True),
                    self.traverse(item.subscript, 0, called=True),
                )
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "Cast":
            stmt = " " * indent + "{0}({1})".format(
                self.traverse(item.to_type, 0, called=True),
                self.traverse(item.expr, 0, called=True),
            )
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "Typename":
            stmt = " " * indent + "{0}".format(item.type.type.names[0])
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "ID":
            if item.name == "true":
                name = "True"
            elif item.name == "false":
                name = "False"
            else:
                name = item.name
            stmt = " " * indent + "{0}".format(name)
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "Compound":
            stmt = ""
            if called:
                for i in range(len(item.block_items)):
                    stmt += (
                        self.traverse(item.block_items[i], indent, called=True) + "\n"
                    )
            else:
                for i in range(len(item.block_items)):
                    stmt += (
                        self.traverse(item.block_items[i], indent + 4, called=True)
                        + "\n"
                    )
            return stmt[:-1]
        elif item.__class__.__name__ == "TernaryOp":
            stmt = " " * indent + "{0} if {1} else {2}".format(
                self.traverse(item.iftrue, 0, called=True),
                self.traverse(item.cond, 0, called=True),
                self.traverse(item.iffalse, 0, called=True),
            )
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "While":
            stmt = " " * indent + "while {0}:\n".format(
                self.traverse(item.cond, 0, called=True)
            )
            for i in range(len(item.stmt.block_items)):
                stmt += (
                    self.traverse(item.stmt.block_items[i], indent + 4, called=True)
                    + "\n"
                )
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "StructRef":
            stmt = " " * indent + "{0}{1}{2}".format(
                self.traverse(item.name, 0, called=True),
                item.type,
                self.traverse(item.field, 0, called=True),
            )
            if called:
                return stmt
            else:
                self.code += stmt
        elif item.__class__.__name__ == "EmptyStatement":
            pass
        else:
            raise Exception("Unable to parse {0}".format(item.__class__.__name__))


class FuncDecl(object):
    def __init__(self, ast, typelist):
        self.ast = ast
        self.name = ast.name
        self.typelist = typelist[self.name]
        self.args = []
        self.returntype = self.ast.type.type.type.names[0]
        self.traverse()

    def traverse(self):
        if self.ast.type.args is not None:
            params = self.ast.type.args.params
            for param in params:
                typename, listcount = self.iterclass(param.type, 0)
                self.args.append(
                    {"name": param.name, "type": typename, "list": listcount}
                )

    def iterclass(self, obj, count):
        if obj.__class__.__name__ == "IdentifierType":
            return obj.names[0], count
        elif obj.__class__.__name__ == "TypeDecl":
            return self.iterclass(obj.type, count)
        elif obj.__class__.__name__ == "PtrDecl":
            return self.iterclass(obj.type, count + 1)

    def arrange_args(self, types=False, labels=False):
        assert not (types and labels)
        arranged = ""
        for i in range(len(self.args)):
            if i != 0:
                arranged += ", "
            if types:
                if self.args[i]["name"] in self.typelist.keys():
                    self.args[i]["type"] = self.typelist[self.args[i]["name"]]
                arranged += (
                    "{0}: ".format(self.args[i]["name"])
                    + "List[" * self.args[i]["list"]
                    + self.args[i]["type"]
                    + "]" * self.args[i]["list"]
                )
            elif labels:
                arranged += "{0}: {1}".format(
                    self.args[i]["name"], labels[self.args[i]["name"]]
                )
            else:
                arranged += "{0} ".format(self.args[i]["name"])
        return arranged


def remove_return(code):
    if code[code.rfind("\n", 0, code.rfind("\n")) :].strip() == "return":
        k = code.rfind("return")
        code = code[:k] + code[k + 6 :]
    return code


def arrange_args(templateargs):
    arranged = ""
    for i in range(len(templateargs)):
        if i != 0:
            arranged += ", "
        arranged += templateargs[i]
    return arranged


def indent_code(code, indent):
    finalcode = ""
    for line in code.splitlines():
        finalcode += " " * indent + line + "\n"
    return finalcode


if __name__ == "__main__":

    def getheadername(filename):
        if "/" in filename:
            hfile = filename[filename.rfind("/") + 1 : -4] + ".h"
        else:
            hfile = filename[:-4] + ".h"
        hfile = os.path.join(
            CURRENT_DIR, "..", "include", "awkward", "cpu-kernels", hfile
        )
        return hfile

    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("filenames", nargs="+")
    args = arg_parser.parse_args()
    filenames = args.filenames
    doc_awkward_sorting_ranges = """
    def awkward_sorting_ranges(toindex, tolength, parents,  parentslength):
        j = 0
        k = 0
        toindex[0] = k
        k = k + 1
        j = j + 1
        for i in range(1, parentslength):
            if parents[i-1] != parents[i]:
                toindex[j] = k
                j = j + 1
            k = k + 1
        toindex[tolength - 1] = parentslength
    """
    doc_awkward_sorting_ranges_length = """
    def awkward_sorting_ranges_length(tolength, parents, parentslength):
        length = 2
        for i in range(1, parentslength):
            if parents[i-1] != parents[i]:
                length = length + 1
        tolength[0] = length
    """
    doc_awkward_argsort = """
    def awkward_argsort(toptr, fromptr, length, offsets, offsetslength, ascending, stable):
        result = [0]*length
        for i in range(length):
            result[i] = i
        for i in range(offsetslength - 1):
            if ascending:
                result[offsets[i]:offsets[i + 1]] = [ x for _, x in sorted(zip(fromptr[offsets[i]:offsets[i+1]], result[offsets[i]:offsets[i+1]]))]
            else:
                result[offsets[i]:offsets[i + 1]] = [ x for _, x in sorted(zip(fromptr[offsets[i]:offsets[i+1]], result[offsets[i]:offsets[i+1]]), reverse=True)]
            for j in range(offsets[i], offsets[i+1]):
                result[j] = result[j] - offsets[i]
        for i in range(length):
            toptr[i] = result[i]
    """
    doc_awkward_sort = """
    def awkward_sort(toptr, fromptr, length, offsets, offsetslength, parentslength, ascending, stable):
        index = [0]*length
        for i in range(length):
            index[i] = i
        for i in range(offsetslength - 1):
            if ascending:
                index[offsets[i]:offsets[i + 1]] = [ x for _, x in sorted(zip(fromptr[offsets[i]:offsets[i+1]], index[offsets[i]:offsets[i+1]]))]
            else:
                index[offsets[i]:offsets[i + 1]] = [ x for _, x in sorted(zip(fromptr[offsets[i]:offsets[i+1]], index[offsets[i]:offsets[i+1]]), reverse=True)]
        for i in range(parentslength):
            toptr[i] = fromptr[index[i]]
    """
    doc_awkward_ListOffsetArray_local_preparenext_64 = """
    def awkward_ListOffsetArray_local_preparenext_64(tocarry, fromindex, length):
        result = [0]*length
        for i in range(length):
            result[i] = i
        result = [ x for _, x in sorted(zip(fromindex, result))]
        for i in range(length):
            tocarry[i] = result[i]
    """
    doc_awkward_IndexedArray_local_preparenext_64 = """
    def awkward_IndexedArray_local_preparenext_64(tocarry, starts, parents, parentsoffset, parentslength, nextparents, nextparentsoffset):
        j = 0
        for i in range(parentslength):
            parent = parents[i] + parentsoffset
            start = starts[parent]
            nextparent = nextparents[j] + nextparentsoffset
            if parent == nextparent:
                tocarry[i] = j
                j = j + 1
            else:
                tocarry[i] = -1
    """
    doc_awkward_NumpyArray_sort_asstrings_uint8 = """
    def awkward_NumpyArray_sort_asstrings_uint8(toptr, fromptr, offsets, offsetslength, outoffsets, ascending, stable):
        words = []
        for k in range(offsetslength - 1):
            start = offsets[k]
            stop = offsets[k + 1]
            slen = copy.copy(start)
            strvar = ""
            i = copy.copy(start)
            while (slen < stop):
                slen = slen + 1
                strvar = strvar + str(fromptr[i])
            words.append(strvar)
        if ascending:
            words.sort()
        else:
            words.sort(reverse=True)
        k = 0
        for strvar in words:
            cstr = [ch for ch in strvar]
            for c in cstr:
                toptr[k] = c
                k = k + 1
        o = 0
        outoffsets[o] = 0
        o = o + 1
        for r in words:
            outoffsets[o] = outoffsets[o - 1] + len(r)
            o = o + 1
    """
    blackmode = black.FileMode()  # Initialize black config

    # Preface of generated Python
    gencode = """import copy

kMaxInt64  = 9223372036854775806
kSliceNone = kMaxInt64 + 1

outparam = None
inparam = None
inoutparam = None

"""
    failfuncs = []
    docdict = OrderedDict()
    func_callable = OrderedDict()
    allhtokens = {}
    for filename in filenames:
        if "sorting.cpp" in filename:
            pfile, tokens = preprocess(filename, skip_implementation=True)
        else:
            pfile, tokens = preprocess(filename)
        failfuncs.extend(check_fail_func(filename))
        ast = pycparser.c_parser.CParser().parse(pfile)
        funcs = OrderedDict()
        for i in range(len(ast.ext)):
            decl = FuncDecl(ast.ext[i].decl, tokens)
            funcs[decl.name] = OrderedDict()
            funcs[decl.name]["def"] = decl
            if "sorting.cpp" not in filename:
                body = FuncBody(ast.ext[i].body)
                funcs[decl.name]["body"] = body
        hfile = getheadername(filename)
        htokens = parseheader(hfile)
        allhtokens.update(htokens)
        for name in funcs.keys():
            if "gen" not in tokens[name].keys():
                docdict[name] = {}
                docprefix = name + "\n"
                docprefix += "=================================================================\n"
                if "childfunc" in tokens[name].keys():
                    for childfunc in tokens[name]["childfunc"]:
                        docprefix += (
                            ".. py:function:: {0}({1})".format(
                                funcs[childfunc]["def"].name,
                                funcs[childfunc]["def"].arrange_args(types=True),
                            )
                            + "\n\n"
                        )
                else:
                    func_callable[name] = tokens[name]
                    funcs[name]["def"].arrange_args(types=True)
                    func_callable[name]["args"] = funcs[name]["def"].args
                if "sorting.cpp" not in filename:
                    d = OrderedDict()
                    if "childfunc" in tokens[name].keys():
                        for x in htokens.keys():
                            if x in tokens[name]["childfunc"]:
                                for key, val in htokens[x].items():
                                    d[key] = val["check"]
                    else:
                        if name in htokens.keys():
                            for key, val in htokens[name].items():
                                d[key] = val["check"]
                    tokens[name]["labels"] = d
                    funcdef = (
                        "def {0}({1})".format(
                            name,
                            funcs[name]["def"].arrange_args(
                                labels=tokens[name]["labels"]
                            ),
                        )
                        + ":\n"
                    )
                    funcbody = remove_return(funcs[name]["body"].code)
                else:
                    docprefix += "*(The following Python code is translated from C++ manually and may not be normative)*\n\n"
                funcgentemp = ""
                if "childfunc" in tokens[name].keys():
                    for childfunc in tokens[name]["childfunc"]:
                        funcgentemp += "{0} = {1}\n".format(
                            funcs[childfunc]["def"].name, name
                        )
                docprefix += ".. code-block:: python\n\n"
                docdict[name]["prefix"] = docprefix
                if "sorting.cpp" not in filename:
                    funcbody = funcbody + funcgentemp
                    docdict[name]["def"] = indent_code(funcdef, 4) + "\n"
                    docdict[name]["body"] = funcbody + "\n\n"
                    gencode += funcdef + funcbody + "\n"
                else:
                    funcgen = (
                        black.format_str(eval("doc_" + name), mode=blackmode) + "\n\n"
                    )
                    docdict[name]["def"] = ""
                    docdict[name]["body"] = indent_code(funcgen, 4) + funcgentemp + "\n"
                    gencode += funcgen + funcgentemp + "\n"
            else:
                func_callable[name] = tokens[name]
                func_callable[name]["args"] = funcs[name]["def"].args
        # Put roles as Python definition docstrings
        temptokens = gettokens(func_callable, htokens)
        if "sorting.cpp" not in filename:
            for name in funcs.keys():
                rolestring = ""
                if "gen" not in tokens[name].keys():
                    if "childfunc" in tokens[name].keys():
                        roletokens = temptokens[list(tokens[name]["childfunc"])[0]]
                    else:
                        if name in htokens.keys():
                            roletokens = temptokens[name]
                    for key, val in roletokens.items():
                        if val["array"] or val["check"] == "outparam":
                            rolestring += "# " + key + " - " + val["check"]
                            if "role" in val.keys():
                                rolestring += " role: " + val["role"]
                            rolestring += "\n"
                    docdict[name]["def"] += rolestring
    with open(os.path.join(CURRENT_DIR, "kernels.py"), "w") as f:
        print("Writing kernels.py")
        f.write(gencode)
        f.write(black.format_str(gencode, mode=blackmode) + "\n\n")
    gentests(func_callable, allhtokens, failfuncs)
    if os.path.isdir(os.path.join(CURRENT_DIR, "..", "docs-sphinx", "_auto")):
        with open(
            os.path.join(CURRENT_DIR, "..", "docs-sphinx", "_auto", "kernels.rst",),
            "w",
        ) as f:
            print("Writing kernels.rst")
            f.write(
                """Kernel interface and specification
----------------------------------

All array manipulation takes place in the lowest layer of the Awkward Array project, the "kernels." The primary implementation of these kernels are in ``libawkward-cpu-kernels.so`` (or similar names on MacOS and Windows), which has a pure C interface.

A second implementation, ``libawkward-cuda-kernels.so``, is provided as a separate package, ``awkward1-cuda``, which handles arrays that reside on GPUs if CUDA is available. It satisfies the same C interface and implements the same behaviors.

.. raw:: html

    <img src="../_static/awkward-1-0-layers.svg" style="max-width: 500px; margin-left: auto; margin-right: auto;">

The interface, as well as specifications for each function's behavior through a normative Python implementation, are presented below.

"""
            )
            for name in sorted(docdict.keys()):
                f.write(docdict[name]["prefix"])
                f.write(
                    indent_code(
                        black.format_str(
                            docdict[name]["def"] + docdict[name]["body"], mode=blackmode
                        ),
                        4,
                    )
                    + "\n\n"
                )
        if os.path.isfile(
            os.path.join(CURRENT_DIR, "..", "docs-sphinx", "_auto", "toctree.txt",)
        ):
            with open(
                os.path.join(CURRENT_DIR, "..", "docs-sphinx", "_auto", "toctree.txt",),
                "r+",
            ) as f:
                if "_auto/kernels.rst" not in f.read():
                    print("Updating toctree.txt")
                    f.write(" " * 4 + "_auto/kernels.rst")
