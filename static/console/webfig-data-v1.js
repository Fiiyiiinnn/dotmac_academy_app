/* Academy WebFig data grammar v1. Load the pinned Acorn browser asset first. */
(function (root) {
    "use strict";

    if (!root.acorn || root.acorn.version !== "8.19.0" ||
        typeof root.acorn.Parser !== "function" || typeof root.acorn.Parser.extend !== "function") {
        throw new TypeError("WebFig data requires the pinned Acorn parser");
    }
    // Acorn's official extension hook intercepts both tokenizer and expression
    // reparsing paths before readRegexp can validate or compile a regex. Comment
    // skipping remains Acorn's responsibility and never enters this hook.
    const DataParser = root.acorn.Parser.extend(function (Parser) {
        return class extends Parser {
            readRegexp() {
                throw new SyntaxError("WebFig data forbids regular expressions");
            }
        };
    });
    // Parser.parse uses its class as `this`; bind the captured method once so
    // later changes to Acorn's public parse methods cannot replace this parser.
    const parseSyntax = DataParser.parse.bind(DataParser);
    const MAX_BYTES = 2 * 1024 * 1024;
    const MAX_NODES = 500000;
    const MAX_DEPTH = 128;
    const MAX_TOKENS = MAX_NODES * 4 + 8;
    const MAX_AST_WORK = MAX_NODES * 3;
    const encoder = new TextEncoder();

    function checkedText(text) {
        if (typeof text !== "string") {
            throw new TypeError("WebFig data must be a string");
        }
        if (text.length > MAX_BYTES || encoder.encode(text).byteLength > MAX_BYTES) {
            throw new RangeError("WebFig data exceeds the UTF-8 byte limit");
        }
        return text;
    }

    function plainObject(value) {
        return value !== null && typeof value === "object" &&
            Object.getPrototypeOf(value) === Object.prototype;
    }

    function tokenBudget() {
        let tokens = 0;
        let previous = "";
        let closed = false;
        const delimiters = [];
        // This callback only inspects consumed tokens; it never reenters Acorn.
        return function (token) {
            const label = token.type.label;
            tokens += 1;
            if (tokens > MAX_TOKENS) {
                throw new RangeError("WebFig data exceeds the token limit");
            }
            if (label === "eof") {
                return;
            }
            if (closed) {
                throw new SyntaxError("WebFig data has a trailing expression");
            }
            if (previous === "+/-" && label !== "num") {
                throw new SyntaxError("WebFig signs require a single numeric literal");
            }
            if (label === "(") {
                // Only our artificial outer parentheses are permitted. This
                // prevents call/group/function recursion before AST validation.
                if (tokens !== 1) {
                    throw new SyntaxError("WebFig data forbids nested parentheses");
                }
                delimiters.push(label);
            } else if (label === "[" || label === "{") {
                delimiters.push(label);
                if (delimiters.length > MAX_DEPTH + 1) {
                    throw new RangeError("WebFig data exceeds the delimiter depth limit");
                }
            } else if (label === "]" || label === "}" || label === ")") {
                const expected = label === "]" ? "[" : label === "}" ? "{" : "(";
                if (delimiters.pop() !== expected) {
                    throw new SyntaxError("WebFig data has mismatched delimiters");
                }
                if (label === ")") {
                    closed = true;
                }
            } else if (label === "+/-") {
                // Reject binary operators and unary chains while parsing, not
                // after Acorn has recursively constructed their ASTs.
                if (previous !== "[" && previous !== ":" && previous !== ",") {
                    throw new SyntaxError("WebFig data permits only numeric unary signs");
                }
            } else if (label !== "num" && label !== "string" && label !== "name" &&
                       label !== "true" && label !== "false" && label !== "null" &&
                       label !== "," && label !== ":") {
                // ES5 keyword property names are valid data keys. Restrict all
                // other keyword positions to stop new/typeof/void/delete chains.
                if (!token.type.keyword || (previous !== "{" && previous !== ",")) {
                    throw new SyntaxError("WebFig data contains executable syntax");
                }
            }
            previous = label;
        };
    }

    function literal(node) {
        if (node.type !== "Literal" || node.regex !== undefined || node.bigint !== undefined) {
            throw new TypeError("WebFig data requires a primitive literal");
        }
        const value = node.value;
        if (value === null || typeof value === "string" || typeof value === "boolean" ||
            (typeof value === "number" && Number.isFinite(value))) {
            return value;
        }
        throw new TypeError("WebFig data contains a non-finite or non-data literal");
    }

    function propertyKey(property) {
        if (property.type !== "Property" || property.kind !== "init" ||
            property.method || property.shorthand || property.computed) {
            throw new TypeError("WebFig data forbids accessors and executable properties");
        }
        const keyNode = property.key;
        let key;
        if (keyNode.type === "Identifier") {
            key = keyNode.name;
        } else {
            const value = literal(keyNode);
            if (typeof value !== "string" && typeof value !== "number") {
                throw new TypeError("WebFig data property keys must be names, strings or numbers");
            }
            key = String(value);
        }
        if (key === "__proto__" || key === "prototype" || key === "constructor") {
            throw new TypeError("WebFig data contains a forbidden property");
        }
        return key;
    }

    function materialize(expression) {
        const result = {};
        const pending = [{node: expression, parent: result, key: "value", depth: 1}];
        let nodes = 1;
        let work = 0;
        while (pending.length) {
            const current = pending.pop();
            const node = current.node;
            work += 1;
            if (work > MAX_AST_WORK) {
                throw new RangeError("WebFig data exceeds the AST work limit");
            }
            let value;
            if (node.type === "Literal") {
                value = literal(node);
            } else if (node.type === "UnaryExpression") {
                work += 1;
                if (work > MAX_AST_WORK) {
                    throw new RangeError("WebFig data exceeds the AST work limit");
                }
                if (!node.prefix || (node.operator !== "+" && node.operator !== "-") ||
                    node.argument.type !== "Literal") {
                    throw new TypeError("WebFig data permits only signed numeric literals");
                }
                const number = literal(node.argument);
                if (typeof number !== "number") {
                    throw new TypeError("WebFig signs require a numeric literal");
                }
                value = node.operator === "-" ? -number : number;
            } else if (node.type === "ArrayExpression" || node.type === "ObjectExpression") {
                const array = node.type === "ArrayExpression";
                const children = array ? node.elements : node.properties;
                // Charge all scheduled values before allocation or adding jobs.
                nodes += children.length;
                if (nodes > MAX_NODES) {
                    throw new RangeError("WebFig data exceeds the node limit");
                }
                if (children.length && current.depth >= MAX_DEPTH) {
                    throw new RangeError("WebFig data exceeds the depth limit");
                }
                work += array ? 0 : children.length * 2; // Property and key nodes.
                if (work > MAX_AST_WORK) {
                    throw new RangeError("WebFig data exceeds the AST work limit");
                }
                value = array ? new Array(children.length) : {};
                // LIFO jobs must visit source order so duplicate data keys keep
                // their last value, matching ordinary object literal semantics.
                for (let index = children.length - 1; index >= 0; index -= 1) {
                    let child = children[index];
                    let key = String(index);
                    if (array) {
                        if (child === null) {
                            throw new TypeError("WebFig data forbids array holes");
                        }
                    } else {
                        key = propertyKey(child);
                        child = child.value;
                    }
                    pending.push({node: child, parent: value, key, depth: current.depth + 1});
                }
            } else {
                throw new TypeError("WebFig data contains a forbidden AST node: " + node.type);
            }
            Object.defineProperty(current.parent, current.key, {
                value, enumerable: true, configurable: true, writable: true,
            });
        }
        return result.value;
    }

    function parseData(text) {
        const program = parseSyntax("(" + text + "\n)", {
            ecmaVersion: 5,
            sourceType: "script",
            onToken: tokenBudget(),
        });
        if (program.type !== "Program" || program.body.length !== 1 ||
            program.body[0].type !== "ExpressionStatement") {
            throw new TypeError("WebFig data must contain exactly one expression");
        }
        return materialize(program.body[0].expression);
    }

    function parseList(text) {
        checkedText(text);
        // Preserve the empty final object filtered by the original caller.
        const value = parseData("[" + text + "{}]");
        if (!Array.isArray(value) || !value.every(plainObject)) {
            throw new TypeError("WebFig list must contain objects");
        }
        return value;
    }

    function parseSchema(text) {
        const value = parseData(checkedText(text));
        if (!Array.isArray(value)) {
            throw new TypeError("WebFig schema must be an array");
        }
        return value;
    }

    function parseSkin(text) {
        checkedText(text);
        const value = text.trim() === "" ? {} : parseData(text);
        if (!plainObject(value)) {
            throw new TypeError("WebFig skin must be an object");
        }
        return value;
    }

    root.AcademyWebFigDataV1 = Object.freeze({parseList, parseSchema, parseSkin});
}(globalThis));
