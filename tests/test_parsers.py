from kg.parsers.java import base_type, parse_java
from kg.parsers.manifests import parse_pom, parse_spring_config, parse_tsconfig
from kg.parsers.typescript import parse_ts


def test_base_type():
    assert base_type("List<Map<String, Foo>>") == "List"
    assert base_type("Foo[]") == "Foo"
    assert base_type("final com.acme.Foo<X>") == "com.acme.Foo"
    assert base_type("String...") == "String"


JAVA = b'''
package com.acme.x;
import com.acme.y.Repo;
import static com.acme.z.Util.helper;
/** Does things. */
@RestController
@RequestMapping(path = {"/a", "/b"})
public class Ctl extends Base implements Api, Other<String> {
    static final String P = "/v1" + "/items";
    private final Repo repo;
    @GetMapping(P + "/{id}")
    public Item get(@PathVariable Long id, String... rest) {
        var tmp = new Holder();
        tmp.go();
        repo.load(id).map(x -> x);
        helper();
        Other.staticCall();
        return this.local(1);
    }
    class Inner { void f() { repo.load(1); } }
}
'''


def test_java_declarations_and_calls():
    r = parse_java(JAVA)
    assert r["package"] == "com.acme.x"
    assert r["static_imports"] == {"helper": "com.acme.z.Util"}
    ctl, inner = r["types"]
    assert ctl["fqn"] == "com.acme.x.Ctl" and inner["fqn"] == "com.acme.x.Ctl.Inner"
    assert ctl["doc"] == "Does things."
    assert ctl["extends"] == ["Base"] and ctl["implements"] == ["Api", "Other<String>"]
    rm = next(a for a in ctl["annotations"] if a["name"] == "RequestMapping")
    assert rm["args"]["path"] == ["/a", "/b"]
    assert ctl["consts"] == {"P": "/v1/items"}
    m = ctl["methods"][0]
    assert m["params"][1]["varargs"] is True
    calls = {c["name"]: c["recv"] for c in m["calls"]}
    assert calls["go"] == {"type": "Holder", "var": "tmp"}          # `var` inferred from `new`
    assert calls["load"] == {"type": "Repo", "var": "repo"}          # field type via scope
    assert calls["map"] == {"chain": "load"}
    assert calls["helper"] == {"type": "com.acme.z.Util", "static": True}
    assert calls["staticCall"] == {"type": "Other", "static": True}
    assert calls["local"] == {"this": True}
    assert m["creates"] == ["Holder"]
    # inner class members are not attributed to the outer method
    assert [c["name"] for c in inner["methods"][0]["calls"]] == ["load"]


def test_java_mapping_with_constant_concatenation():
    r = parse_java(JAVA)
    get = r["types"][0]["methods"][0]
    mapping = next(a for a in get["annotations"] if a["name"] == "GetMapping")
    # same-class String constants are folded into mapping paths
    assert mapping["args"]["value"] == ["/v1/items/{id}"]


def test_ts_routes_api_calls_exports():
    src = b'''
import api from "./api";
const BASE = "/api/v2";
export const routes = [{ path: "/a", element: <A/>, children: [{ path: "b", Component: B }] }];
export default function Page() {
  api.post(`${BASE}/items/${id}`, body);
  axios({ url: "/x/y", method: "put" });
  return <Routes><Route path="/p"><Route path="q" element={<Q/>}/></Route></Routes>;
}
export { Page as Main };
'''
    r = parse_ts(src, tsx=True)
    page = next(s for s in r["symbols"] if s["name"] == "Page")
    assert page["is_component"]
    assert {(a["method"], a["url"]) for a in page["api_calls"]} == {("POST", "/api/v2/items/{}"), ("PUT", "/x/y")}
    assert sorted((x["path"], x["component"]) for x in page["routes"]) == [("/p", None), ("/p/q", "Q")]
    assert sorted((x["path"], x["component"]) for x in r["module"]["routes"]) == [("/a", "A"), ("/a/b", "B")]
    assert r["exports"]["default"] == "Page" and r["exports"]["Main"] == "Page"


def test_pom_property_substitution():
    pom = b'''<project xmlns="http://maven.apache.org/POM/4.0.0">
      <parent><groupId>com.acme</groupId><artifactId>parent</artifactId><version>2</version></parent>
      <artifactId>svc</artifactId>
      <properties><lib.version>9.9</lib.version></properties>
      <dependencies><dependency><groupId>${project.groupId}</groupId><artifactId>lib</artifactId>
        <version>${lib.version}</version></dependency></dependencies></project>'''
    p = parse_pom(pom)
    assert (p["group"], p["artifact"], p["version"]) == ("com.acme", "svc", "2")
    assert p["dependencies"][0] == {"group": "com.acme", "artifact": "lib", "version": "9.9", "scope": "compile"}


def test_spring_config_keeps_keys_not_values():
    y = b"spring:\n  application:\n    name: svc\n  datasource:\n    password: hunter2\n---\nfoo:\n  bar: 1\n"
    c = parse_spring_config(y, is_properties=False)
    assert c["application_name"] == "svc"
    assert "spring.datasource.password" in c["keys"] and "foo.bar" in c["keys"]
    assert "hunter2" not in repr(c)


def test_tsconfig_with_comments_and_trailing_commas():
    t = parse_tsconfig(b'{ /* c */ "compilerOptions": { "baseUrl": "src", // x\n "paths": {"@/*": ["*"],}, }, }')
    assert t["baseUrl"] == "src" and t["paths"] == {"@/*": ["*"]}
