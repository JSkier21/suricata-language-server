import logging
import os
import traceback
import re

from suricatals.jsonrpc import path_to_uri, path_from_uri
from suricatals.parse_signatures import SuricataFile
from suricatals.tests_rules import TestRules

log = logging.getLogger(__name__)

SURICATA_RULES_EXT_REGEX = re.compile(r'^\.rules?$', re.I)

def init_file(filepath, suricata_binary, pp_defs, pp_suffixes, include_dirs):
    #
    file_obj = SuricataFile(filepath, pp_suffixes, suricata_binary=suricata_binary)
    err_str = file_obj.load_from_disk()
    if err_str is not None:
        return None, err_str
    #
    try:
        file_obj.parse_file()
    except:
        log.error("Error while parsing file %s", filepath, exc_info=True)
        return None, 'Error during parsing'
    return file_obj, None


class LangServer:
    def __init__(self, conn, debug_log=False, settings={}):
        self.conn = conn
        self.running = True
        self.root_path = None
        self.fs = None
        self.all_symbols = None
        self.workspace = {}
        self.obj_tree = {}
        self.link_version = 0
        self.source_dirs = []
        self.excl_paths = []
        self.excl_suffixes = []
        self.post_messages = []
        self.pp_suffixes = None
        self.pp_defs = {}
        self.include_dirs = []
        self.streaming = True
        self.debug_log = debug_log
        # FIXME
        self.debug_log = True
        # Get launch settings
        self.nthreads = settings.get("nthreads", 4)
        self.notify_init = settings.get("notify_init", False)
        self.sync_type = settings.get("sync_type", 1)
        self.suricata_binary = settings.get("suricata_binary", 'suricata')
        self.keywords_list = TestRules(suricata_binary=self.suricata_binary).build_keywords_list()

    def post_message(self, message, type=1):
        self.conn.send_notification("window/showMessage", {
            "type": type,
            "message": message
        })

    def run(self):
        # Run server
        while self.running:
            try:
                request = self.conn.read_message()
                self.handle(request)
            except EOFError:
                break
            except Exception as e:
                log.error("Unexpected error: %s", e, exc_info=True)
                break
            else:
                for message in self.post_messages:
                    self.post_message(message[1], message[0])
                self.post_messages = []

    def handle(self, request):
        def noop(request):
            return None
        # Request handler
        log.debug("REQUEST %s %s", request.get("id"), request.get("method"))
        handler = {
            "initialize": self.serve_initialize,
            "textDocument/documentSymbol": self.serve_document_symbols,
            "textDocument/completion": self.serve_autocomplete,
            "textDocument/signatureHelp": self.serve_signature,
            "textDocument/definition": self.serve_definition,
            "textDocument/references": self.serve_references,
            "textDocument/hover": self.serve_hover,
            "textDocument/implementation": self.serve_implementation,
            "textDocument/rename": self.serve_rename,
            "textDocument/didOpen": self.serve_onOpen,
            "textDocument/didSave": self.serve_onSave,
            "textDocument/didClose": self.serve_onClose,
            "textDocument/didChange": self.serve_onChange,
            "textDocument/codeAction": self.serve_codeActions,
            "initialized": noop,
            "workspace/didChangeWatchedFiles": noop,
            "workspace/symbol": self.serve_workspace_symbol,
            "$/cancelRequest": noop,
            "shutdown": noop,
            "exit": self.serve_exit,
        }.get(request["method"], self.serve_default)
        # handler = {
        #     "workspace/symbol": self.serve_symbols,
        # }.get(request["method"], self.serve_default)
        # We handle notifications differently since we can't respond
        if "id" not in request:
            try:
                handler(request)
            except:
                log.warning(
                    "error handling notification %s", request, exc_info=True)
            return
        #
        try:
            resp = handler(request)
        except JSONRPC2Error as e:
            self.conn.write_error(
                request["id"], code=e.code, message=e.message, data=e.data)
            log.warning("RPC error handling request %s", request, exc_info=True)
        except Exception as e:
            self.conn.write_error(
                request["id"],
                code=-32603,
                message=str(e),
                data={
                    "traceback": traceback.format_exc(),
                })
            log.warning("error handling request %s", request, exc_info=True)
        else:
            self.conn.write_response(request["id"], resp)

    def serve_initialize(self, request):
        # Setup language server
        params = request["params"]
        self.root_path = path_from_uri(
            params.get("rootUri") or params.get("rootPath") or "")
        self.source_dirs.append(self.root_path)
        # Check for config file
        config_path = os.path.join(self.root_path, ".suricatals")
        config_exists = os.path.isfile(config_path)
        if config_exists:
            try:
                import json
                with open(config_path, 'r') as fhandle:
                    config_dict = json.load(fhandle)
                    for excl_path in config_dict.get("excl_paths", []):
                        self.excl_paths.append(os.path.join(self.root_path, excl_path))
                    source_dirs = config_dict.get("source_dirs", [])
                    ext_source_dirs = config_dict.get("ext_source_dirs", [])
                    # Legacy definition
                    if len(source_dirs) == 0:
                        source_dirs = config_dict.get("mod_dirs", [])
                    for source_dir in source_dirs:
                        dir_path = os.path.join(self.root_path, source_dir)
                        if os.path.isdir(dir_path):
                            self.source_dirs.append(dir_path)
                        else:
                            self.post_messages.append(
                                [2, r'Source directory "{0}" specified in '
                                 r'".suricatals" settings file does not exist'.format(dir_path)]
                            )
                    for ext_source_dir in ext_source_dirs:
                        if os.path.isdir(ext_source_dir):
                            self.source_dirs.append(ext_source_dir)
                        else:
                            self.post_messages.append(
                                [2, r'External source directory "{0}" specified in '
                                 r'".suricatals" settings file does not exist'.format(ext_source_dir)]
                            )
                    if isinstance(self.pp_defs, list):
                        self.pp_defs = {key: "" for key in self.pp_defs}
            except:
                self.post_messages.append([1, 'Error while parsing ".suricatals" settings file'])
            # Make relative include paths absolute
            for (i, include_dir) in enumerate(self.include_dirs):
                if not os.path.isabs(include_dir):
                    self.include_dirs[i] = os.path.abspath(os.path.join(self.root_path, include_dir))
        # Recursively add sub-directories
        if len(self.source_dirs) == 1:
            self.source_dirs = []
            for dirName, subdirList, fileList in os.walk(self.root_path):
                if self.excl_paths.count(dirName) > 0:
                    while(len(subdirList) > 0):
                        del subdirList[0]
                    continue
                contains_source = False
                for filename in fileList:
                    _, ext = os.path.splitext(os.path.basename(filename))
                    if SURICATA_RULES_EXT_REGEX.match(ext):
                        contains_source = True
                        break
                if contains_source:
                    self.source_dirs.append(dirName)
        # Initialize workspace
        self.workspace_init()
        #
        server_capabilities = {
            "completionProvider": {
                "resolveProvider": False,
                "triggerCharacters": ["%"]
            },
            #"definitionProvider": True,
            #"documentSymbolProvider": True,
            #"referencesProvider": True,
            #"hoverProvider": True,
            #"implementationProvider": True,
            #"renameProvider": True,
            #"workspaceSymbolProvider": True,
            "textDocumentSync": self.sync_type
        }
        if self.notify_init:
            self.post_messages.append([3, "suricatals initialization complete"])
        return {"capabilities": server_capabilities}
        #     "workspaceSymbolProvider": True,
        #     "streaming": False,
        # }

    def serve_workspace_symbol(self, request):
        def map_types(type):
            if type == 1:
                return 2
            elif type == 2:
                return 6
            elif type == 3:
                return 12
            elif type == 4:
                return 5
            elif type == 5:
                return 11
            elif type == 6:
                return 13
            elif type == 7:
                return 6
            else:
                return 1
        matching_symbols = []
        query = request["params"]["query"].lower()
        for candidate in find_in_workspace(self.obj_tree, query):
            tmp_out = {
                "name": candidate.name,
                "kind": map_types(candidate.get_type()),
                "location": {
                    "uri": path_to_uri(candidate.file_ast.path),
                    "range": {
                        "start": {"line": candidate.sline-1, "character": 0},
                        "end": {"line": candidate.eline-1, "character": 0}
                    }
                }
            }
            # Set containing scope
            if candidate.FQSN.find('::') > 0:
                tmp_list = candidate.FQSN.split("::")
                tmp_out["containerName"] = tmp_list[0]
            matching_symbols.append(tmp_out)
        return sorted(matching_symbols, key=lambda k: k['name'])

    def serve_document_symbols(self, request):
        def map_types(type, in_class=False):
            if type == 1:
                return 2
            elif (type == 2) or (type == 3):
                if in_class:
                    return 6
                else:
                    return 12
            elif type == 4:
                return 5
            elif type == 5:
                return 11
            elif type == 6:
                return 13
            elif type == 7:
                return 6
            else:
                return 1
        # Get parameters from request
        params = request["params"]
        uri = params["textDocument"]["uri"]
        path = path_from_uri(uri)
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return []
        # Add scopes to outline view
        test_output = []
        for scope in file_obj.ast.get_scopes():
            if (scope.name[0] == "#") or (scope.get_type() == SELECT_TYPE_ID):
                continue
            scope_tree = scope.FQSN.split("::")
            if len(scope_tree) > 2:
                if scope_tree[1].startswith("#gen_int"):
                    scope_type = 11
                else:
                    continue
            else:
                scope_type = map_types(scope.get_type())
            tmp_out = {}
            tmp_out["name"] = scope.name
            tmp_out["kind"] = scope_type
            sline = scope.sline-1
            eline = scope.eline-1
            tmp_out["location"] = {
                "uri": uri,
                "range": {
                    "start": {"line": sline, "character": 0},
                    "end": {"line": eline, "character": 0}
                }
            }
            # Set containing scope
            if scope.FQSN.find('::') > 0:
                tmp_list = scope.FQSN.split("::")
                tmp_out["containerName"] = tmp_list[0]
            test_output.append(tmp_out)
            # If class add members
            if (scope.get_type() == CLASS_TYPE_ID) and self.symbol_include_mem:
                for child in scope.children:
                    tmp_out = {}
                    tmp_out["name"] = child.name
                    tmp_out["kind"] = map_types(child.get_type(), True)
                    tmp_out["location"] = {
                        "uri": uri,
                        "range": {
                            "start": {"line": child.sline-1, "character": 0},
                            "end": {"line": child.sline-1, "character": 0}
                        }
                    }
                    tmp_out["containerName"] = scope.name
                    test_output.append(tmp_out)
        return test_output

    def serve_autocomplete(self, request):
        params = request["params"]
        uri = params["textDocument"]["uri"]
        path = path_from_uri(uri)
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return None
        sig_content = file_obj.line_content_map[params['position']['line']]
        sig_index = params['position']['character'] 
        log.debug(sig_content)
        cursor = sig_index - 1
        while cursor > 0:
            log.debug("At index: %d of %d (%s)" % (cursor, len(sig_content), sig_content[cursor:sig_index]))
            if not sig_content[cursor].isalnum() and not sig_content[cursor] in ['.', '_']:
                break
            cursor -= 1
        log.debug("Final is: %d : %d" % (cursor, sig_index))
        if cursor == sig_index - 1:
            return None
        cursor += 1
        partial_keyword = sig_content[cursor:sig_index]
        log.debug("Got keyword start: '%s'" % (partial_keyword))
        items_list = []
        for item in self.keywords_list:
            if item['label'].startswith(partial_keyword):
                items_list.append(item)
        if len(items_list):
            return items_list
        return None

    def get_definition(self, def_file, def_line, def_char):
        # Get full line (and possible continuations) from file
        pre_lines, curr_line, _ = def_file.get_code_line(def_line, forward=False, strip_comment=True)
        line_prefix = get_line_prefix(pre_lines, curr_line, def_char)
        if line_prefix is None:
            return None
        is_member = False
        try:
            var_stack = get_var_stack(line_prefix)
            is_member = (len(var_stack) > 1)
            def_name = expand_name(curr_line, def_char)
        except:
            return None
        # print(var_stack, def_name)
        if def_name == '':
            return None
        curr_scope = def_file.ast.get_inner_scope(def_line+1)
        # Traverse type tree if necessary
        if is_member:
            type_scope = climb_type_tree(var_stack, curr_scope, self.obj_tree)
            # Set enclosing type as scope
            if type_scope is None:
                return None
            else:
                curr_scope = type_scope
        # Find in available scopes
        var_obj = None
        if curr_scope is not None:
            if (curr_scope.get_type() == CLASS_TYPE_ID) and (not is_member) and \
               ((line_prefix.lstrip().lower().startswith('procedure') and (line_prefix.count("=>") > 0))
               or TYPE_DEF_REGEX.match(line_prefix)):
                curr_scope = curr_scope.parent
            var_obj = find_in_scope(curr_scope, def_name, self.obj_tree)
        # Search in global scope
        if var_obj is None:
            if is_member:
                return None
            key = def_name.lower()
            if key in self.obj_tree:
                return self.obj_tree[key][0]
            for obj in self.intrinsic_funs:
                if obj.name.lower() == key:
                    return obj
        else:
            return var_obj
        return None

    def serve_signature(self, request):
        def get_sub_name(line):
            _, sections = get_paren_level(line)
            if sections[0][0] <= 1:
                return None, None, None
            arg_string = line[sections[0][0]:sections[-1][1]]
            sub_string, sections = get_paren_level(line[:sections[0][0]-1])
            return sub_string.strip(), arg_string.split(','), sections[-1][0]

        def check_optional(arg, params):
            opt_split = arg.split("=")
            if len(opt_split) > 1:
                opt_arg = opt_split[0].strip().lower()
                for i, param in enumerate(params):
                    param_split = param["label"].split("=")[0]
                    if param_split.lower() == opt_arg:
                        return i
            return None
        # Get parameters from request
        params = request["params"]
        uri = params["textDocument"]["uri"]
        path = path_from_uri(uri)
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return None
        # Check line
        sig_line = params["position"]["line"]
        sig_char = params["position"]["character"]
        # Get full line (and possible continuations) from file
        pre_lines, curr_line, _ = file_obj.get_code_line(sig_line, forward=False, strip_comment=True)
        line_prefix = get_line_prefix(pre_lines, curr_line, sig_char)
        if line_prefix is None:
            return None
        # Test if scope declaration or end statement
        if SCOPE_DEF_REGEX.match(curr_line) or END_REGEX.match(curr_line):
            return None
        is_member = False
        try:
            sub_name, arg_strings, sub_end = get_sub_name(line_prefix)
            var_stack = get_var_stack(sub_name)
            is_member = (len(var_stack) > 1)
        except:
            return None
        #
        curr_scope = file_obj.ast.get_inner_scope(sig_line+1)
        # Traverse type tree if necessary
        if is_member:
            type_scope = climb_type_tree(var_stack, curr_scope, self.obj_tree)
            # Set enclosing type as scope
            if type_scope is None:
                curr_scope = None
            else:
                curr_scope = type_scope
        sub_name = var_stack[-1]
        # Find in available scopes
        var_obj = None
        if curr_scope is not None:
            var_obj = find_in_scope(curr_scope, sub_name, self.obj_tree)
        # Search in global scope
        if var_obj is None:
            key = sub_name.lower()
            if key in self.obj_tree:
                var_obj = self.obj_tree[key][0]
            else:
                for obj in self.intrinsic_funs:
                    if obj.name.lower() == key:
                        var_obj = obj
                        break
        # Check keywords
        if (var_obj is None) and (INT_STMNT_REGEX.match(line_prefix[:sub_end]) is not None):
            key = sub_name.lower()
            for candidate in get_intrinsic_keywords(self.statements, self.keywords, 0):
                if candidate.name.lower() == key:
                    var_obj = candidate
                    break
        if var_obj is None:
            return None
        # Build signature
        label, doc_str, params = var_obj.get_signature()
        if label is None:
            return None
        # Find current parameter by index or by
        # looking at last arg with optional name
        param_num = len(arg_strings)-1
        opt_num = check_optional(arg_strings[-1], params)
        if opt_num is None:
            if len(arg_strings) > 1:
                opt_num = check_optional(arg_strings[-2], params)
                if opt_num is not None:
                    param_num = opt_num + 1
        else:
            param_num = opt_num
        signature = {
            "label": label,
            "parameters": params
        }
        if doc_str is not None:
            signature["documentation"] = doc_str
        req_dict = {
            "signatures": [signature],
            "activeParameter": param_num
        }
        return req_dict

    def get_all_references(self, def_obj, type_mem, file_obj=None):
        # Search through all files
        def_name = def_obj.name.lower()
        def_fqsn = def_obj.FQSN
        NAME_REGEX = re.compile(r'(?:\W|^)({0})(?:\W|$)'.format(def_name), re.I)
        if file_obj is None:
            file_set = self.workspace.items()
        else:
            file_set = ((file_obj.path, file_obj), )
        override_cache = []
        refs = {}
        ref_objs = []
        for filename, file_obj in file_set:
            file_refs = []
            # Search through file line by line
            for (i, line) in enumerate(file_obj.contents_split):
                if len(line) == 0:
                    continue
                # Skip comment lines
                line = file_obj.strip_comment(line)
                if (line == '') or (line[0] == '#'):
                    continue
                for match in NAME_REGEX.finditer(line):
                    var_def = self.get_definition(file_obj, i, match.start(1)+1)
                    if var_def is not None:
                        ref_match = False
                        if (def_fqsn == var_def.FQSN) or (var_def.FQSN in override_cache):
                            ref_match = True
                        elif var_def.parent.get_type() == CLASS_TYPE_ID:
                            if type_mem:
                                for inherit_def in var_def.parent.get_overriden(def_name):
                                    if def_fqsn == inherit_def.FQSN:
                                        ref_match = True
                                        override_cache.append(var_def.FQSN)
                                        break
                            if (var_def.sline-1 == i) and (var_def.file_ast.path == filename) \
                               and (line.count("=>") == 0):
                                try:
                                    if var_def.link_obj is def_obj:
                                        ref_objs.append(var_def)
                                        ref_match = True
                                except:
                                    pass
                        if ref_match:
                            file_refs.append([i, match.start(1), match.end(1)])
            if len(file_refs) > 0:
                refs[filename] = file_refs
        return refs, ref_objs

    def serve_references(self, request):
        # Get parameters from request
        params = request["params"]
        uri = params["textDocument"]["uri"]
        def_line = params["position"]["line"]
        def_char = params["position"]["character"]
        path = path_from_uri(uri)
        # Find object
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return None
        def_obj = self.get_definition(file_obj, def_line, def_char)
        if def_obj is None:
            return None
        # Determine global accesibility and type membership
        restrict_file = None
        type_mem = False
        if def_obj.FQSN.count(":") > 2:
            if def_obj.parent.get_type() == CLASS_TYPE_ID:
                type_mem = True
            else:
                restrict_file = def_obj.file_ast.file
                if restrict_file is None:
                    return None
        all_refs, _ = self.get_all_references(def_obj, type_mem, file_obj=restrict_file)
        refs = []
        for (filename, file_refs) in all_refs.items():
            for ref in file_refs:
                refs.append({
                    "uri": path_to_uri(filename),
                    "range": {
                        "start": {"line": ref[0], "character": ref[1]},
                        "end": {"line": ref[0], "character": ref[2]}
                    }
                })
        return refs

    def serve_definition(self, request):
        # Get parameters from request
        params = request["params"]
        uri = params["textDocument"]["uri"]
        def_line = params["position"]["line"]
        def_char = params["position"]["character"]
        path = path_from_uri(uri)
        # Find object
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return None
        var_obj = self.get_definition(file_obj, def_line, def_char)
        if var_obj is None:
            return None
        # Construct link reference
        if var_obj.file_ast.file is not None:
            var_file = var_obj.file_ast.file
            sline, schar, echar = \
                var_file.find_word_in_code_line(var_obj.sline-1, var_obj.name)
            if schar < 0:
                schar = echar = 0
            return {
                "uri": path_to_uri(var_file.path),
                "range": {
                    "start": {"line": sline, "character": schar},
                    "end": {"line": sline, "character": echar}
                }
            }
        return None

    def serve_hover(self, request):
        def create_hover(string, highlight):
            if highlight:
                return {
                    "language": self.hover_language,
                    "value": string
                }
            else:
                return string
        # Get parameters from request
        params = request["params"]
        uri = params["textDocument"]["uri"]
        def_line = params["position"]["line"]
        def_char = params["position"]["character"]
        path = path_from_uri(uri)
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return None
        # Find object
        var_obj = self.get_definition(file_obj, def_line, def_char)
        if var_obj is None:
            return None
        # Construct hover information
        var_type = var_obj.get_type()
        hover_array = []
        if (var_type == SUBROUTINE_TYPE_ID) or (var_type == FUNCTION_TYPE_ID):
            hover_str, highlight = var_obj.get_hover(long=True)
            hover_array.append(create_hover(hover_str, highlight))
        elif var_type == INTERFACE_TYPE_ID:
            for member in var_obj.mems:
                hover_str, highlight = member.get_hover(long=True)
                if hover_str is not None:
                    hover_array.append(create_hover(hover_str, highlight))
            return {"contents": hover_array}
        elif self.variable_hover and (var_type == 6):
            hover_str, highlight = var_obj.get_hover()
            hover_array.append(create_hover(hover_str, highlight))
            if self.hover_signature:
                sig_request = request.copy()
                sig_result = self.serve_signature(sig_request)
                try:
                    arg_id = sig_result.get("activeParameter")
                    if arg_id is not None:
                        arg_info = sig_result["signatures"][0]["parameters"][arg_id]
                        arg_doc = arg_info["documentation"]
                        doc_split = arg_doc.find("\n !!")
                        if doc_split < 0:
                            arg_string = "{0} :: {1}".format(arg_doc, arg_info["label"])
                        else:
                            arg_string = "{0} :: {1}{2}".format(arg_doc[:doc_split],
                                                                arg_info["label"], arg_doc[doc_split:])
                        hover_array.append(create_hover(arg_string, True))
                except:
                    pass
        #
        if len(hover_array) > 0:
            return {"contents": hover_array}
        return None

    def serve_implementation(self, request):
        # Get parameters from request
        params = request["params"]
        uri = params["textDocument"]["uri"]
        def_line = params["position"]["line"]
        def_char = params["position"]["character"]
        path = path_from_uri(uri)
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return None
        # Find object
        var_obj = self.get_definition(file_obj, def_line, def_char)
        if var_obj is None:
            return None
        # Construct implementation reference
        if var_obj.parent.get_type() == CLASS_TYPE_ID:
            impl_obj = var_obj.link_obj
            if (impl_obj is not None) and (impl_obj.file_ast.file is not None):
                impl_file = impl_obj.file_ast.file
                sline, schar, echar = \
                    impl_file.find_word_in_code_line(impl_obj.sline-1, impl_obj.name)
                if schar < 0:
                    schar = echar = 0
                return {
                    "uri": path_to_uri(impl_file.path),
                    "range": {
                        "start": {"line": sline, "character": schar},
                        "end": {"line": sline, "character": echar}
                    }
                }
        return None

    def serve_rename(self, request):
        # Get parameters from request
        params = request["params"]
        uri = params["textDocument"]["uri"]
        def_line = params["position"]["line"]
        def_char = params["position"]["character"]
        path = path_from_uri(uri)
        # Find object
        file_obj = self.workspace.get(path)
        if file_obj is None:
            return None
        def_obj = self.get_definition(file_obj, def_line, def_char)
        if def_obj is None:
            return None
        # Determine global accesibility and type membership
        restrict_file = None
        type_mem = False
        if def_obj.FQSN.count(":") > 2:
            if def_obj.parent.get_type() == CLASS_TYPE_ID:
                type_mem = True
            else:
                restrict_file = def_obj.file_ast.file
                if restrict_file is None:
                    return None
        all_refs, ref_objs = self.get_all_references(def_obj, type_mem, file_obj=restrict_file)
        if len(all_refs) == 0:
            self.post_message('Rename failed: No usages found to rename', type=2)
            return None
        # Create rename changes
        new_name = params["newName"]
        changes = {}
        for (filename, file_refs) in all_refs.items():
            file_uri = path_to_uri(filename)
            changes[file_uri] = []
            for ref in file_refs:
                changes[file_uri].append({
                    "range": {
                        "start": {"line": ref[0], "character": ref[1]},
                        "end": {"line": ref[0], "character": ref[2]}
                    },
                    "newText": new_name
                })
        # Check for implicit procedure implementation naming
        bind_obj = None
        if def_obj.get_type(no_link=True) == METH_TYPE_ID:
            _, curr_line, post_lines = def_obj.file_ast.file.get_code_line(
                def_obj.sline-1, backward=False, strip_comment=True
            )
            if curr_line is not None:
                full_line = curr_line + ''.join(post_lines)
                if full_line.find('=>') < 0:
                    bind_obj = def_obj
                    bind_change = "{0} => {1}".format(new_name, def_obj.name)
        elif (len(ref_objs) > 0) and (ref_objs[0].get_type(no_link=True) == METH_TYPE_ID):
            bind_obj = ref_objs[0]
            bind_change = "{0} => {1}".format(ref_objs[0].name, new_name)
        # Replace definition statement with explicit implementation naming
        if bind_obj is not None:
            def_uri = path_to_uri(bind_obj.file_ast.file.path)
            for change in changes[def_uri]:
                if change['range']['start']['line'] == bind_obj.sline-1:
                    change["newText"] = bind_change
        return {"changes": changes}

    def serve_codeActions(self, request):
        params = request["params"]
        uri = params["textDocument"]["uri"]
        sline = params["range"]["start"]["line"]
        eline = params["range"]["end"]["line"]
        path = path_from_uri(uri)
        file_obj = self.workspace.get(path)
        # Find object
        if file_obj is None:
            return None
        curr_scope = file_obj.ast.get_inner_scope(sline)
        if curr_scope is None:
            return None
        action_list = curr_scope.get_actions(sline, eline)
        if action_list is None:
            return None
        # Convert diagnostics
        for action in action_list:
            diagnostics = action.get("diagnostics")
            if diagnostics is not None:
                new_diags = []
                for diagnostic in diagnostics:
                    new_diags.append(diagnostic.build(file_obj))
                action["diagnostics"] = new_diags
        return action_list

    def send_diagnostics(self, uri):
        diag_results, diag_exp = self.get_diagnostics(uri)
        if diag_results is not None:
            self.conn.send_notification("textDocument/publishDiagnostics", {
                "uri": uri,
                "diagnostics": diag_results
            })
        elif diag_exp is not None:
            self.conn.write_error(
                -1,
                code=-32603,
                message=str(diag_exp),
                data={
                    "traceback": traceback.format_exc(),
                })

    def get_diagnostics(self, uri):
        filepath = path_from_uri(uri)
        file_obj = self.workspace.get(filepath)
        if file_obj is not None:
            try:
                diags = file_obj.check_file(self.obj_tree)
            except Exception as e:
                return None, e
            else:
                return diags, None
        return None, None

    def serve_onChange(self, request):
        # Update workspace from file sent by editor
        params = request["params"]
        uri = params["textDocument"]["uri"]
        path = path_from_uri(uri)
        file_obj = self.workspace.get(path)
        if file_obj is None:
            self.post_message('Change request failed for unknown file "{0}"'.format(path))
            log.error('Change request failed for unknown file "%s"', path)
            return
        else:
            # Update file contents with changes
            reparse_req = True
            if self.sync_type == 1:
                file_obj.apply_change(params["contentChanges"][0])
            else:
                try:
                    reparse_req = False
                    for change in params["contentChanges"]:
                        reparse_flag = file_obj.apply_change(change)
                        reparse_req = (reparse_req or reparse_flag)
                except:
                    self.post_message('Change request failed for file "{0}": Could not apply change'.format(path))
                    log.error('Change request failed for file "%s": Could not apply change', path, exc_info=True)
                    return
        # Parse newly updated file
        if reparse_req:
            _, err_str = self.update_workspace_file(path, update_links=True)
            if err_str is not None:
                self.post_message('Change request failed for file "{0}": {1}'.format(path, err_str))
                return

    def serve_onOpen(self, request):
        self.serve_onSave(request, did_open=True)

    def serve_onClose(self, request):
        self.serve_onSave(request, did_close=True)

    def serve_onSave(self, request, did_open=False, did_close=False):
        # Update workspace from file on disk
        params = request["params"]
        uri = params["textDocument"]["uri"]
        filepath = path_from_uri(uri)
        # Skip update and remove objects if file is deleted
        if did_close and (not os.path.isfile(filepath)):
            # Remove old objects from tree
            file_obj = self.workspace.get(filepath)
            if file_obj is not None:
                ast_old = file_obj.ast
                if ast_old is not None:
                    for key in ast_old.global_dict:
                        self.obj_tree.pop(key, None)
            return
        did_change, err_str = self.update_workspace_file(filepath, read_file=True, allow_empty=did_open)
        if err_str is not None:
            self.post_message('Save request failed for file "{0}": {1}'.format(filepath, err_str))
            return
        if did_change:
            file_obj = self.workspace.get(filepath)
        self.send_diagnostics(uri)

    def update_workspace_file(self, filepath, read_file=False, allow_empty=False, update_links=False):
        # Update workspace from file contents and path
        try:
            file_obj = self.workspace.get(filepath)
            if read_file:
                if file_obj is None:
                    file_obj = SuricataFile(filepath, self.pp_suffixes, suricata_binary=self.suricata_binary)
                    # Create empty file if not yet saved to disk
                    if not os.path.isfile(filepath):
                        if allow_empty:
                            file_obj.ast = fortran_ast(file_obj)
                            self.workspace[filepath] = file_obj
                            return False, None
                        else:
                            return False, 'File does not exist'  # Error during load
                hash_old = file_obj.hash
                err_string = file_obj.load_from_disk()
                if err_string is not None:
                    log.error(err_string + ": %s", filepath)
                    return False, err_string  # Error during file read
                if hash_old == file_obj.hash:
                    return False, None
            file_obj.parse_file()
        except:
            log.error("Error while parsing file %s", filepath, exc_info=True)
            return False, 'Error during parsing'  # Error during parsing
        if filepath not in self.workspace:
            self.workspace[filepath] = file_obj
        return True, None

    def workspace_init(self):
        # Get filenames
        file_list = []
        for source_dir in self.source_dirs:
            for filename in os.listdir(source_dir):
                _, ext = os.path.splitext(os.path.basename(filename))
                if SURICATA_RULES_EXT_REGEX.match(ext):
                    filepath = os.path.normpath(os.path.join(source_dir, filename))
                    if self.excl_paths.count(filepath) > 0:
                        continue
                    inc_file = True
                    for excl_suffix in self.excl_suffixes:
                        if filepath.endswith(excl_suffix):
                            inc_file = False
                            break
                    if inc_file:
                        file_list.append(filepath)
        # Process files
        from multiprocessing import Pool
        pool = Pool(processes=self.nthreads)
        results = {}
        for filepath in file_list:
            results[filepath] = pool.apply_async(init_file, args=(
                filepath, self.suricata_binary, self.pp_defs, self.pp_suffixes, self.include_dirs
            ))
        pool.close()
        pool.join()
        for path, result in results.items():
            result_obj = result.get()
            if result_obj[0] is None:
                self.post_messages.append([1, 'Initialization failed for file "{0}": {1}'.format(path, result_obj[1])])
                continue
            self.workspace[path] = result_obj[0]

    def serve_exit(self, request):
        # Exit server
        self.workspace = {}
        self.obj_tree = {}
        self.running = False

    def serve_default(self, request):
        # Default handler (errors!)
        raise JSONRPC2Error(
            code=-32601,
            message="method {} not found".format(request["method"]))


class JSONRPC2Error(Exception):
    def __init__(self, code, message, data=None):
        self.code = code
        self.message = message
        self.data = data
