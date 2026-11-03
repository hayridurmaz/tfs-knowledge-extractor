"""End-to-end graph construction over the fixture repos (order-service, customer-service, common-lib, web-app)."""
from conftest import edges

O = "java:order-service:com.acme.order"
CTL = f"{O}.api.OrderController"
SVC = f"{O}.service.OrderService"
IMPL = f"{O}.service.OrderServiceImpl"
WEB = "ts:web-app:src"


def test_spring_endpoints(graph):
    eps = {n.props["http_method"] + " " + n.props["path"] for n in graph.nodes.values() if n.label == "Endpoint"}
    assert eps == {"GET /api/orders/{id}", "POST /api/orders", "GET /api/orders/search", "GET /api/orders/find",
                   "GET /customers/{customerId}/exists", r"GET /customers/{customerId:\d+}"}
    assert edges(graph, "EXPOSES", f"{CTL}#get(Long)", "ep:order-service:GET:/api/orders/{id}")
    assert graph.nodes[f"ep:order-service:GET:/api/orders/{{id}}"].props["service_names"] == ["order-service"]


def test_java_calls_and_dispatch(graph):
    e = edges(graph, "CALLS", f"{CTL}#get(Long)", f"{SVC}#find(Long)")
    assert e and e[0].props["confidence"] == 0.95
    assert edges(graph, "OVERRIDES", f"{IMPL}#find(Long)", f"{SVC}#find(Long)")
    assert edges(graph, "CALLS", f"{IMPL}#place(Order)", f"{IMPL}#validate(Order)")
    # static import resolved across repos
    assert edges(graph, "CALLS", f"{IMPL}#place(Order)", "java:common-lib:com.acme.common.Money#zero()")
    # calls into external libraries (JpaRepository.save) are not invented
    assert not [x for x in edges(graph, "CALLS", f"{IMPL}#place(Order)") if x.dst.endswith("#save(Order)")]


def test_spring_semantics(graph):
    assert {e.dst for e in edges(graph, "INJECTS", IMPL)} == {f"{O}.domain.OrderRepository", f"{O}.client.CustomerClient"}
    assert edges(graph, "INJECTS", CTL, SVC)  # constructor injection
    assert edges(graph, "REPOSITORY_OF", f"{O}.domain.OrderRepository", f"{O}.domain.Order")
    assert edges(graph, "MAPS_TO", f"{O}.domain.Order", "table:orders")
    assert edges(graph, "MAPS_TO", "java:customer-service:com.acme.customer.Customer", "table:customer")
    assert edges(graph, "READS_CONFIG", f"{IMPL}#paymentTimeout", "cfg:order-service:app.payment.timeout-ms")
    assert edges(graph, "PUBLISHES", f"{IMPL}#place(Order)", "topic:order-created")
    assert edges(graph, "SUBSCRIBES", f"{IMPL}#onPayment(String)", "topic:payment-completed")
    assert "JpaEntity" in graph.nodes[f"{O}.domain.Order"].extra_labels
    assert "Controller" in graph.nodes[CTL].extra_labels


def test_cross_repo_http(graph):
    feign = edges(graph, "CONSUMES", f"{O}.client.CustomerClient#exists(Long)")
    assert [(e.dst, e.props["confidence"]) for e in feign] == [("ep:customer-service:GET:/customers/{customerId}/exists", 0.95)]
    by_line = {e.src.rsplit(":", 1)[1]: e.dst for e in edges(graph, "CONSUMES") if e.src.startswith("api:web-app")}
    assert by_line == {"8": "ep:order-service:GET:/api/orders/{id}",       # template literal + const
                       "12": "ep:order-service:POST:/api/orders",           # fetch with method option
                       "16": "ep:order-service:GET:/api/orders/search"}     # query string stripped


def test_react_graph(graph):
    assert edges(graph, "ROUTES_TO", "route:web-app:/orders/:id", f"{WEB}/pages/OrderPage.tsx#OrderPage")
    assert edges(graph, "RENDERS", f"{WEB}/pages/OrderPage.tsx#OrderPage", f"{WEB}/components/OrderCard.tsx#OrderCard")
    # tsconfig alias '@/hooks/useOrder'
    assert edges(graph, "USES_HOOK", f"{WEB}/pages/OrderPage.tsx#OrderPage", f"{WEB}/hooks/useOrder.ts#useOrder")
    # barrel re-export: '../api' -> api/index.ts -> export * from './client'
    assert edges(graph, "CALLS", f"{WEB}/hooks/useOrder.ts#useOrder", f"{WEB}/api/client.ts#getOrder")
    assert {"Component"} <= graph.nodes[f"{WEB}/App.tsx#App"].extra_labels
    assert "Hook" in graph.nodes[f"{WEB}/hooks/useOrder.ts#useOrder"].extra_labels


def test_dependencies(graph):
    assert edges(graph, "DEPENDS_ON_REPO", "repo:order-service", "repo:common-lib")
    dev = edges(graph, "DEPENDS_ON", "module:web-app:.", "npm:vite")
    assert dev and dev[0].props["scope"] == "dev"


def test_config_values_never_stored(graph):
    assert all("secret-should-not-be-stored" not in repr(n.props) for n in graph.nodes.values())


def test_every_edge_has_both_endpoints_and_repo(graph):
    for e in graph.edges.values():
        assert e.src in graph.nodes and e.dst in graph.nodes
        assert "repo" in e.props
