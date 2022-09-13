import json
from typing import Dict, List
import weakref

import networkx as nx
from gufe import AlchemicalNetwork
from gufe.tokenization import GufeTokenizable
from gufe.storage.metadatastore import MetadataStore
from py2neo import Graph, Node, Relationship, Subgraph
from py2neo.matching import NodeMatcher
from py2neo.errors import ClientError


class Neo4jStore:
    def __init__(self, graph: "py2neo.Graph"):
        self.graph = graph
        self.gufe_nodes = weakref.WeakValueDictionary()

    def _gufe_to_subgraph(
        self, sdct: Dict, labels: List[str], gufe_key, org, campaign, project
    ):

        subgraph = Subgraph()
        node = Node(*labels)

        # used to keep track of which properties we json-encoded so we can
        # apply decoding efficiently
        node["_json_props"] = []
        node["_gufe_key"] = str(gufe_key)
        node.update({"_org": org, "_campaign": campaign, "_project": project})
        node["_scoped_key"] = [node["_gufe_key"], org, campaign, project]

        for key, value in sdct.items():
            if isinstance(value, dict):
                if all([isinstance(x, GufeTokenizable) for x in value.values()]):
                    for k, v in value.items():
                        node_ = subgraph_ = self.gufe_nodes.get(
                            (v.key, org, campaign, project)
                        )
                        if node_ is None:
                            subgraph_, node_ = self._gufe_to_subgraph(
                                v.to_shallow_dict(),
                                labels=["GufeTokenizable", v.__class__.__name__],
                                gufe_key=v.key,
                                org=org,
                                campaign=campaign,
                                project=project,
                            )
                            self.gufe_nodes[
                                (str(v.key), org, campaign, project)
                            ] = node_
                        subgraph = (
                            subgraph
                            | Relationship.type("DEPENDS_ON")(
                                node,
                                node_,
                                attribute=key,
                                key=k,
                                _org=org,
                                _campaign=campaign,
                                _project=project,
                            )
                            | subgraph_
                        )
                else:
                    node[key] = json.dumps(value)
                    node["_json_props"].append(key)
            elif isinstance(value, list):
                # lists can only be made of a single, primitive data type
                # we encode these as strings with a special starting indicator
                if isinstance(value[0], (int, float, str)) and all(
                    [isinstance(x, type(value[0])) for x in value]
                ):
                    node[key] = value
                elif all([isinstance(x, GufeTokenizable) for x in value]):
                    for i, x in enumerate(value):
                        node_ = subgraph_ = self.gufe_nodes.get(
                            (x.key, org, campaign, project)
                        )
                        if node_ is None:
                            subgraph_, node_ = self._gufe_to_subgraph(
                                x.to_shallow_dict(),
                                labels=["GufeTokenizable", x.__class__.__name__],
                                gufe_key=x.key,
                                org=org,
                                campaign=campaign,
                                project=project,
                            )
                            self.gufe_nodes[(x.key, org, campaign, project)] = node_
                        subgraph = (
                            subgraph
                            | Relationship.type("DEPENDS_ON")(
                                node,
                                node_,
                                attribute=key,
                                index=i,
                                _org=org,
                                _campaign=campaign,
                                _project=project,
                            )
                            | subgraph_
                        )
                else:
                    node[key] = json.dumps(value)
                    node["_json_props"].append(key)
            elif isinstance(value, tuple):
                # lists can only be made of a single, primitive data type
                # we encode these as strings with a special starting indicator
                if not (
                    isinstance(value[0], (int, float, str))
                    and all([isinstance(x, type(value[0])) for x in value])
                ):
                    node[key] = json.dumps(value)
                    node["_json_props"].append(key)
            elif isinstance(value, GufeTokenizable):
                node_ = subgraph_ = self.gufe_nodes.get(
                    (value.key, org, campaign, project)
                )
                if node_ is None:
                    subgraph_, node_ = self._gufe_to_subgraph(
                        value.to_shallow_dict(),
                        labels=["GufeTokenizable", value.__class__.__name__],
                        gufe_key=value.key,
                        org=org,
                        campaign=campaign,
                        project=project,
                    )
                    self.gufe_nodes[(value.key, org, campaign, project)] = node_
                subgraph = (
                    subgraph
                    | Relationship.type("DEPENDS_ON")(
                        node,
                        node_,
                        attribute=key,
                        _org=org,
                        _campaign=campaign,
                        _project=project,
                    )
                    | subgraph_
                )
            else:
                node[key] = value

        subgraph = subgraph | node

        return subgraph, node

    def _subgraph_to_gufe(self, nodes: List[Node], subgraph: Subgraph):
        """Get a list of all `GufeTokenizable` objects within the given subgraph.

        Any `GufeTokenizable` that requires nodes or relationships missing from the subgraph will not be returned.

        Returns
        -------
        List[GufeTokenizable]

        """
        nxg = self._subgraph_to_networkx(subgraph)
        # nodes = list(reversed(list(nx.topological_sort(subgraph_to_networkx(sg)))))

        nodes_to_gufe = {}

        gufe_objs = []
        for node in nodes:
            gufe_objs.append(self._node_to_gufe(node, nxg, nodes_to_gufe))

        return gufe_objs

    def _subgraph_to_networkx(self, subgraph: Subgraph):
        g = nx.DiGraph()

        for node in subgraph.nodes:
            g.add_node(node, **dict(node))

        for relationship in subgraph.relationships:
            g.add_edge(
                relationship.start_node, relationship.end_node, **dict(relationship)
            )

        return g

    def _node_to_gufe(
        self, node: Node, g: nx.DiGraph, mapping: Dict[Node, GufeTokenizable]
    ):
        # shortcut if we already have this object deserialized
        if gufe_obj := mapping.get(node):
            return gufe_obj

        dct = dict(node)
        for key, value in dict(node).items():
            # deserialize json-serialized attributes
            if key in dct["_json_props"]:
                dct[key] = json.loads(value)

            # inject dependencies
            dep_edges = g.edges(node)
            postprocess = set()
            for edge in dep_edges:
                u, v = edge
                edgedct = g.get_edge_data(u, v)
                if "attribute" in edgedct:
                    if "key" in edgedct:
                        if not edgedct["attribute"] in dct:
                            dct[edgedct["attribute"]] = dict()
                        dct[edgedct["attribute"]][edgedct["key"]] = self._node_to_gufe(
                            v, g, mapping
                        )
                    elif "index" in edgedct:
                        postprocess.add(edgedct["attribute"])
                        if not edgedct["attribute"] in dct:
                            dct[edgedct["attribute"]] = list()
                        dct[edgedct["attribute"]].append(
                            (edgedct["index"], self._node_to_gufe(v, g, mapping))
                        )
                    else:
                        dct[edgedct["attribute"]] = self._node_to_gufe(v, g, mapping)

        # postprocess any attributes that are lists
        # needed because we don't control the order in which a list is built up
        # but can only order it post-hoc
        for attr in postprocess:
            dct[attr] = [j for i, j in sorted(dct[attr], key=lambda x: x[0])]

        # remove all neo4j-specific keys
        dct.pop("_json_props", None)
        dct.pop("_gufe_key", None)
        dct.pop("_org", None)
        dct.pop("_campaign", None)
        dct.pop("_project", None)
        dct.pop("_scoped_key", None)

        mapping[node] = res = GufeTokenizable.from_shallow_dict(dct)
        return res

    def create_network(self, network: AlchemicalNetwork, org, campaign, project):
        """Add an `AlchemicalNetwork` to the target neo4j database.

        Will give a `ValueError` if any components already exist in the database.
        If this is expected, consider using `update_network` instead.

        """
        g, n = self._gufe_to_subgraph(
            network.to_shallow_dict(),
            labels=["GufeTokenizable", network.__class__.__name__],
            gufe_key=network.key,
            org=org,
            campaign=campaign,
            project=project,
        )

        try:
            self.graph.create(g)
        except ClientError:
            raise ValueError(
                "At least one component of the network already exists in the target database; "
                "consider using `update_network` if this is expected."
            )

    def update_network(self, network: AlchemicalNetwork, org, campaign, project):
        """Add an `AlchemicalNetwork` to the target neo4j database, even if
        some of its components already exist in the database.

        """

        ndict = network.to_shallow_dict()

        g, n = self._gufe_to_subgraph(
            ndict,
            labels=["GufeTokenizable", network.__class__.__name__],
            gufe_key=network.key,
            org=org,
            campaign=campaign,
            project=project,
        )
        self.graph.merge(g, "GufeTokenizable", "_scoped_key")

    def _query_obj(
        self,
        *,
        qualname,
        additional: Dict = None,
        key=None,
        org=None,
        campaign=None,
        project=None,
    ):
        properties = {"_org": org, "_campaign": campaign, "_project": project}

        for (k, v) in list(properties.items()):
            if v is None:
                properties.pop(k)

        if key is not None:
            properties["_gufe_key"] = str(key)

        if additional is None:
            additional = {}
        properties.update(additional)

        prop_string = ", ".join(
            "{}: '{}'".format(key, value) for key, value in properties.items()
        )

        q = f"""
        MATCH p = (n:{qualname} {{{prop_string}}})-[r:DEPENDS_ON*]->(m) 
        WHERE NOT (m)-[:DEPENDS_ON]->()
        RETURN n,p
        """
        nodes = set()
        subgraph = Subgraph()

        for record in self.graph.run(q):
            nodes.add(record["n"])
            subgraph = subgraph | record["p"]

        return self._subgraph_to_gufe(nodes, subgraph)

    def query_networks(
        self, *, name=None, key=None, org=None, campaign=None, project=None
    ):
        """Query for `AlchemicalNetwork`s matching given attributes."""
        additional = {"name": name}
        return self._query_obj(
            qualname="AlchemicalNetwork",
            additional=additional,
            key=key,
            org=org,
            campaign=campaign,
            project=project,
        )

    def query_transformations(
        self, *, name=None, key=None, org=None, campaign=None, project=None
    ):
        return self._query_obj(
            qualname="Transformation",
            key=key,
            org=org,
            campaign=campaign,
            project=project,
        )

    def get_transformations_for_chemicalsystem(self):
        ...

    def get_transformations_result(self):
        ...