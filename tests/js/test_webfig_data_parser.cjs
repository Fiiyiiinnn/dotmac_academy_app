"use strict";

// Hosted CI only: node --test tests/js/test_webfig_data_parser.cjs
// Never evaluate RouterOS response/fixture text as JavaScript.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");
const {TextEncoder} = require("node:util");
const vm = require("node:vm");
const {gunzipSync} = require("node:zlib");

const ROOT = path.resolve(__dirname, "../..");
const PARSER_PATH = "static/console/acorn-8.19.0.js";
const WRAPPER_PATH = "static/console/webfig-data-v1.js";
const MAX_BYTES = 2 * 1024 * 1024;
const read = (file) => fs.readFileSync(path.join(ROOT, file), "utf8");
const serial = (value) => JSON.parse(JSON.stringify(value));

function execute(context, source, filename) {
    return new vm.Script(source, {filename}).runInContext(context, {timeout: 20000});
}

function load(parserSource = read(PARSER_PATH)) {
    const context = vm.createContext({TextEncoder}, {
        codeGeneration: {strings: false, wasm: false},
    });
    execute(context, "window = globalThis; self = globalThis;", "browser-global.js");
    execute(context, parserSource, PARSER_PATH);
    execute(context, read(WRAPPER_PATH), WRAPPER_PATH);
    return {context, api: context.AcademyWebFigDataV1};
}

test("pinned browser asset and license match recorded package provenance", () => {
    const provenance = JSON.parse(read("docs/acorn-console-parser-provenance.json"));
    assert.equal(provenance.package, "acorn");
    assert.equal(provenance.version, "8.19.0");
    assert.equal(provenance.license, "MIT");
    assert.match(provenance.tarball_integrity, /^sha512-/);
    for (const [file, record] of Object.entries(provenance.files)) {
        assert.equal(crypto.createHash("sha256").update(fs.readFileSync(path.join(ROOT, file))).digest("hex"), record.sha256);
    }
});

test("browser load and all parser operations work with string code generation disabled", () => {
    const {context, api} = load();
    assert.equal(execute(context, "Object.isFrozen(AcademyWebFigDataV1)", "api-check.js"), true);
    assert.deepEqual(Object.keys(api).sort(), ["parseList", "parseSchema", "parseSkin"]);
    assert.deepEqual(serial(api.parseList("{name:'ether1',enabled:true,speed:0x10,},")), [
        {name: "ether1", enabled: true, speed: 16}, {},
    ]);
    assert.deepEqual(serial(api.parseList("")), [{}]);
    assert.deepEqual(serial(api.parseSchema("/* schema */ [{type:'string',id:1,}, {nested:[null,false,+1,.5,2.,],},]")), [
        {type: "string", id: 1}, {nested: [null, false, 1, 0.5, 2]},
    ]);
    assert.deepEqual(serial(api.parseSkin("{menu:{label:'Interfaces'},enabled:true,}")), {menu: {label: "Interfaces"}, enabled: true});
    assert.deepEqual(serial(api.parseSkin(" \n\t")), {});
    execute(context, "acorn.parse = acorn.Parser.parse = function () { throw Error('replaced'); }; acorn.Parser.extend = function () { throw Error('replaced'); };", "replace-public-parser.js");
    assert.deepEqual(serial(api.parseSchema("[]")), []);
    assert.throws(() => execute(context, "eval('1')", "deny-eval.js"), /Code generation from strings disallowed/);
    assert.throws(() => execute(context, "Function('return 1')()", "deny-function.js"), /Code generation from strings disallowed/);
    assert.throws(() => execute(context, "new WebAssembly.Module(new Uint8Array([0,97,115,109,1,0,0,0]))", "deny-wasm.js"), /Wasm code generation disallowed/);
});

test("numeric and keyword keys and signed numeric literals retain data semantics", () => {
    const {context, api} = load();
    assert.deepEqual(serial(api.parseSchema("[{icons:{0:116,1:117,0x10:118,1.5:119},function:1,new:2,default:3,typeof:4}]")), [
        {icons: {"0": 116, "1": 117, "16": 118, "1.5": 119}, function: 1, new: 2, default: 3, typeof: 4},
    ]);
    assert.deepEqual(serial(api.parseSchema("[+1,-2,-0x10,-.5]")), [1, -2, -16, -0.5]);
    assert.ok(Object.is(api.parseSchema("[-0]")[0], -0));
    assert.deepEqual(serial(api.parseSkin("{x:1,x:2,}")), {x: 2});
    const data = api.parseSkin("{x:1,0:2}");
    assert.equal(execute(context, "Object.prototype.compromised", "plain-object-prototype.js"), undefined);
    for (const descriptor of Object.values(Object.getOwnPropertyDescriptors(data))) {
        assert.equal(typeof descriptor.get, "undefined");
        assert.equal(typeof descriptor.set, "undefined");
        assert.equal(descriptor.enumerable, true);
    }
});

test("programs and executable expressions are rejected without side effects", () => {
    const {context, api} = load();
    const payloads = [
        "(globalThis.compromised = true)",
        "(()=>{globalThis.compromised = true; return []})()",
        "[function(){globalThis.compromised = true}]",
        "[{get x(){globalThis.compromised = true; return 1}}]",
        "[new Date()]", "[undefined]", "[this]", "[1 + 2]",
        "[];globalThis.compromised=true", "[`${globalThis.compromised=true}`]",
        "[Object.assign({}, {x:1})]", "[RegExp('x')]",
        "[/x/]", "[1n]", "[x.y]", "[{[x]:1}]", "[{x}]", "[{x(){}}]",
        "[...x]", "[{...x}]", "[x=1]", "[x?1:2]", "[x,y]", "[!1]",
        "[~1]", "[typeof 1]", "[void 0]", "[delete x]", "[1*2]", "[1/2]",
        "[1&&2]", "[1||2]", "[1<<2]", "[1==2]", "[x++]", "[--x]",
        "[(1)]", "[,'hole']", "[1,,2]", "[+'2']", "[-true]",
        "[]);globalThis.compromised=true;//", "[] []", "[] {}", "[];[]",
        "({});globalThis.compromised=true;([])", "[1],[]",
    ];
    for (const payload of payloads) {
        assert.throws(() => api.parseSchema(payload), undefined, payload);
    }
    assert.throws(() => api.parseList("{name:(globalThis.compromised=true)},"));
    assert.throws(() => api.parseSkin("{x:(globalThis.compromised=true)}"));
    assert.equal(context.compromised, undefined);
    assert.equal(execute(context, "Object.prototype.compromised", "no-pollution.js"), undefined);
});

test("unsafe keys, non-finite numbers and wrong response shapes are rejected", () => {
    const {context, api} = load();
    for (const key of ["__proto__", "prototype", "constructor", "\\u005f_proto__"]) {
        assert.throws(() => api.parseSchema(`[{"${key}":{compromised:true}}]`));
        assert.throws(() => api.parseSkin(`{nested:{"${key}":1}}`));
        assert.throws(() => api.parseList(`{name:'bad',"${key}":1},`));
    }
    for (const number of ["Infinity", "-Infinity", "+Infinity", "NaN", "-NaN", "1e9999"]) {
        assert.throws(() => api.parseSchema(`[${number}]`));
    }
    for (const input of [null, undefined, 1, {}, [], true]) {
        for (const method of Object.values(api)) assert.throws(() => method(input));
    }
    for (const text of ["{}", "null", "true", "'schema'", "0", ""]) assert.throws(() => api.parseSchema(text));
    for (const text of ["[]", "null", "false", "'skin'", "1"]) assert.throws(() => api.parseSkin(text));
    for (const text of ["1,", "[],", "null,", "'name',", "{name:'missing-comma'}"]) assert.throws(() => api.parseList(text));
    assert.equal(execute(context, "Object.prototype.compromised", "no-prototype-mutation.js"), undefined);
});

test("validation enforces exact byte, depth and total-value bounds", () => {
    const {api} = load();
    assert.equal(api.parseSchema("['" + "a".repeat(MAX_BYTES - 4) + "']")[0].length, MAX_BYTES - 4);
    assert.throws(() => api.parseSchema("['" + "a".repeat(MAX_BYTES - 3) + "']"), /UTF-8 byte limit/);
    assert.throws(() => api.parseSchema("['" + "é".repeat(MAX_BYTES / 2) + "']"), /UTF-8 byte limit/);
    assert.throws(() => api.parseSchema("['" + "😀".repeat(MAX_BYTES / 4) + "']"), /UTF-8 byte limit/);
    assert.throws(() => api.parseSkin(" ".repeat(MAX_BYTES + 1)), /UTF-8 byte limit/);
    assert.throws(() => api.parseList(" ".repeat(MAX_BYTES + 1)), /UTF-8 byte limit/);
    assert.doesNotThrow(() => api.parseSchema("[".repeat(127) + "0" + "]".repeat(127)));
    assert.throws(() => api.parseSchema("[".repeat(128) + "0" + "]".repeat(128)), /depth limit/);
    assert.equal(api.parseSchema("[" + "0,".repeat(499999) + "]").length, 499999);
    assert.throws(() => api.parseSchema("[" + "0,".repeat(500000) + "]"), /node limit/);
});

test("token screening stops deep delimiters and recursive operator chains during parsing", () => {
    const {api} = load();
    assert.throws(() => api.parseSchema("[".repeat(10000) + "0" + "]".repeat(10000)), /delimiter depth limit/);
    assert.throws(() => api.parseSkin("{x:".repeat(10000) + "0" + "}".repeat(10000)), /delimiter depth limit/);
    assert.throws(() => api.parseSchema("[" + "(".repeat(10000) + "1" + ")".repeat(10000) + "]"), /nested parentheses/);
    for (const operator of ["+ ", "- ", "!", "~", "typeof ", "void ", "new "]) {
        assert.throws(() => api.parseSchema("[" + operator.repeat(10000) + "1]"), /WebFig/);
    }
    for (const operator of ["+", "-", "*", "=", "?1:"]) {
        // Acorn can reject an invalid assignment target before consuming its
        // operator. Either refusal must occur without exhausting the JS stack.
        assert.throws(() => api.parseSchema("[1" + (operator + "1").repeat(10000) + "]"),
            (error) => !/stack overflow|call stack/i.test(error.message));
    }
});

test("lexical extension refuses regexes before recursive regex validation while preserving comments", () => {
    const {api} = load();
    for (const source of [
        "[/x/]", "[/" + "(".repeat(10000) + "x" + ")".repeat(10000) + "/]",
        "{x:/" + "[".repeat(10000) + "/}",
    ]) {
        const parse = source.startsWith("{") ? api.parseSkin : api.parseSchema;
        assert.throws(() => parse(source),
            (error) => error.name === "SyntaxError" && error.message === "WebFig data forbids regular expressions");
    }
    assert.deepEqual(serial(api.parseSchema("/* /not-a-regex/ ((( */ [1,// /still-comment/ (((\n2] // trailing /comment/")), [1, 2]);
    assert.deepEqual(serial(api.parseList("// leading /comment/\n{name:'module'}, /* /trailing/ */")), [{name: "module"}, {}]);
    assert.deepEqual(serial(api.parseSkin("{/* /comment/ */x:'/(literal)/',y:1// /comment/\n}")), {x: "/(literal)/", y: 1});
});

test("closed AST materializer rejects every executable node category independently of token screening", () => {
    // The stub returns trusted test ASTs to exercise the materializer in isolation.
    // No response text is executed, even for the intentionally malicious vectors.
    const {context, api} = load("acorn = {version:'8.19.0',Parser:class {static extend(plugin){return plugin(this);} static parse(){return {type:'Program',body:[{type:'ExpressionStatement',expression:candidate}]};}}};");
    const forbiddenTypes = [
        "CallExpression", "MemberExpression", "Identifier", "FunctionExpression",
        "ArrowFunctionExpression", "NewExpression", "AssignmentExpression",
        "BinaryExpression", "LogicalExpression", "ConditionalExpression",
        "SequenceExpression", "UpdateExpression", "ThisExpression", "SpreadElement",
        "TemplateLiteral", "TaggedTemplateExpression", "AwaitExpression", "YieldExpression",
    ];
    for (const type of forbiddenTypes) {
        execute(context, "candidate = " + JSON.stringify({type}), "forbidden-ast-fixture.js");
        assert.throws(() => api.parseSchema("[]"), /forbidden AST node/);
    }
    for (const type of ["get", "set", "method", "shorthand", "computed", "spread"]) {
        const property = {type: "Property", kind: "init", key: {type: "Identifier", name: "x"}, value: {type: "Literal", value: 1}};
        if (type === "get" || type === "set") property.kind = type;
        else if (type === "spread") property.type = "SpreadElement";
        else property[type] = true;
        execute(context, "candidate = " + JSON.stringify({type: "ObjectExpression", properties: [property]}), "executable-property-fixture.js");
        assert.throws(() => api.parseSkin("{}"), /accessors and executable properties/);
    }
    for (const source of [
        "{type:'Literal',value:/x/,regex:{pattern:'x',flags:''}}",
        "{type:'Literal',value:1,bigint:'1'}",
        "{type:'Literal',value:undefined}", "{type:'Literal',value:Infinity}",
        "{type:'Literal',value:function(){}}",
        "{type:'UnaryExpression',prefix:true,operator:'-',argument:{type:'Literal',value:'2'}}",
        "{type:'UnaryExpression',prefix:true,operator:'!',argument:{type:'Literal',value:1}}",
        "{type:'UnaryExpression',prefix:true,operator:'-',argument:{type:'UnaryExpression'}}",
    ]) {
        execute(context, "candidate = " + source, "non-data-ast-fixture.js");
        assert.throws(() => api.parseSchema("[]"));
    }
});

test("all nine captured ROM schemas and actual module list parse as data under strict CSP", () => {
    const manifestPath = path.join(ROOT, "tests/fixtures/webfig/manifest.json");
    assert.ok(fs.existsSync(manifestPath), "captured ROM fixture manifest is required in hosted CI");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    const {api} = load();
    function decodedFixture(fixture) {
        assert.equal(fixture.compression, "gzip");
        assert.ok(Number.isSafeInteger(fixture.bytes) && fixture.bytes > 0 && fixture.bytes <= MAX_BYTES);
        const fixturePath = path.resolve(path.dirname(manifestPath), fixture.path);
        assert.ok(fixturePath.startsWith(path.dirname(manifestPath) + path.sep));
        assert.ok(fs.statSync(fixturePath).size <= MAX_BYTES, "compressed fixture exceeds input bound");
        const bytes = gunzipSync(fs.readFileSync(fixturePath), {maxOutputLength: MAX_BYTES});
        assert.equal(bytes.length, fixture.bytes);
        assert.equal(crypto.createHash("sha256").update(bytes).digest("hex"), fixture.sha256);
        return bytes.toString("utf8");
    }
    assert.equal(Object.keys(manifest.assets).length, 42);
    assert.equal(manifest.schemas.length, 9);
    const moduleList = api.parseList(decodedFixture(manifest.assets["/webfig/list"]));
    assert.equal(moduleList.length, 13);
    assert.deepEqual(serial(moduleList[moduleList.length - 1]), {});
    const schemaModules = moduleList.filter((entry) => typeof entry.unique === "string" && entry.unique.endsWith(".jg"));
    assert.equal(schemaModules.length, 9);
    const listedHashes = new Set(schemaModules.map((entry) => manifest.assets["/webfig/" + entry.unique].sha256));
    assert.equal(listedHashes.size, 9);
    for (const fixture of manifest.schemas) {
        assert.ok(listedHashes.delete(fixture.sha256), "every captured schema must occur exactly once in the real list");
        const schema = api.parseSchema(decodedFixture(fixture));
        assert.ok(Array.isArray(schema) && schema.length > 0, fixture.path);
    }
    assert.equal(listedHashes.size, 0);
});

test("skin JSON and legacy object-literal data vectors parse with code generation disabled", () => {
    const {api} = load();
    // Skin responses are session-specific and are not part of the static ROM
    // capture. These explicit data vectors model loadSkin/normalizeSkin inputs.
    const expected = {"Interfaces": {_hide: 1}, "IP": {"Addresses": {_hide: 1}, order: 0}, order: 2};
    assert.deepEqual(serial(api.parseSkin(JSON.stringify(expected))), expected);
    assert.deepEqual(serial(api.parseSkin("{'Interfaces':{_hide:1},IP:{Addresses:{_hide:1},order:0,},order:2,}")), expected);
    assert.deepEqual(serial(api.parseSkin("{}")), {});
    assert.deepEqual(serial(api.parseSkin("\n \t")), {});
});
