#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download resource and project XML data from Topology;
use the data to create a JSON file (project_resource_allocation.json)
for looking up resource allocations for projects.

In a separate JSON file (resource_info_lookups.json), put dicts for easier
lookups of common queries, such as "resource name by FQDN".  Everything
(including the XML files) will be saved in a local directory.

See the README.md file for examples.

There's some redundancy in the information (e.g. fqdn is included in the
`resources_by_fqdn` entries) but having a consistent entry format makes
things easier to read (and was easier to implement).


"""
from argparse import ArgumentParser
from collections import namedtuple
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET


from urllib.request import urlopen

from typing import Optional


TOPOLOGY = "https://topology.opensciencegrid.org"


log = logging.getLogger(__name__)


class DataError(Exception):
    pass


class ResourceInfo(namedtuple("ResourceInfo", "group_name name fqdn service_ids tags")):
    SERVICE_ID_CE = "1"
    SERVICE_ID_SCHEDD = "109"

    def is_ce(self):
        return self.SERVICE_ID_CE in self.service_ids

    def is_schedd(self):
        return self.SERVICE_ID_SCHEDD in self.service_ids


class TopologyData:
    def __init__(
        self,
        topology_base=TOPOLOGY,
    ):
        if "://" not in topology_base:
            topology_base = "https://" + topology_base
        self.topology_base = topology_base
        self.resinfo_table = []
        self.grouped_resinfo = {}
        self.resinfo_by_name = {}
        self.resinfo_by_fqdn = {}
        self.projects = None
        self.resources = None
        self.update_projects()
        self.update_resources()

    def update_projects(self):
        self.projects = self._get_data("/miscproject/xml")

    def update_resources(self):
        self.resources = self._get_data("/rgsummary/xml")
        self.resinfo_table = []
        self.grouped_resinfo = {}
        self.resinfo_by_name = {}
        self.resinfo_by_fqdn = {}

        #
        # Build tables and indices for easier lookup
        #
        for eResourceGroup in self.resources.findall("./ResourceGroup"):
            group_name = safe_element_text(eResourceGroup.find("./GroupName"))
            if not group_name:
                log.warning(
                    "Skipping malformed ResourceGroup: %s", elem2str(eResourceGroup)
                )
                continue
            self.grouped_resinfo[group_name] = []

            for eResource in eResourceGroup.findall("./Resources/Resource"):
                resource_name = safe_element_text(eResource.find("./Name"))
                fqdn = safe_element_text(eResource.find("./FQDN"))
                service_ids = findall_nonempty(eResource, "./Services/Service/ID")
                if not resource_name or not fqdn or not service_ids:
                    log.warning("Skipping malformed Resource: %s", elem2str(eResource))
                    continue
                tags = findall_nonempty(eResource, "./Tags/Tag")
                resinfo = ResourceInfo(
                    group_name, resource_name, fqdn, service_ids, tags
                )
                self.resinfo_table.append(resinfo)
                self.grouped_resinfo[group_name].append(resinfo)
                self.resinfo_by_name[resource_name] = resinfo
                self.resinfo_by_fqdn[fqdn] = resinfo

    def get_resource_info_lookups(self):
        """Return a dict with 3 items (intended to go into a single .json file):
        "resource_lists_by_group":
            {
                "resource_group1": [ ResourceInfo1, ... ],
                "resource_group2": [ ResourceInfo2, ... ],
            },
        "resources_by_name":
            {
                "resource1": ResourceInfo1,
                "resource2": ResourceInfo2,
                ...
            },
        "resources_by_fqdn":
            {
                "resfqdn1.example.net": ResourceInfo1,
                "resfqdn2.example.net": ResourceInfo2,
                ...
            }

        """

        resource_lists_by_group = {}
        for group, resinfo_list in self.grouped_resinfo.items():
            resource_lists_by_group[group] = [x._asdict() for x in resinfo_list]
        resources_by_name = {k: v._asdict() for k, v in self.resinfo_by_name.items()}
        resources_by_fqdn = {k: v._asdict() for k, v in self.resinfo_by_fqdn.items()}
        return {
            "resource_lists_by_group": resource_lists_by_group,
            "resources_by_name": resources_by_name,
            "resources_by_fqdn": resources_by_fqdn,
        }

    def get_project_resource_allocations(self):
        """Combines projects data (from self.projects) and resource data (from self.resources)
        into a dict keyed by Project Name; see README.md for the full format.

        """
        if not self.projects or not self.resources:
            return {}

        ret = {}
        for eProject in self.projects.findall("./Project"):
            project_name = safe_element_text(eProject.find("./Name"))
            if not project_name:
                log.warning(
                    "Project has a missing or empty Name: %s", elem2str(eProject)
                )
                continue

            ret[project_name] = allocations = []
            for eResourceAllocation in eProject.findall(
                "./ResourceAllocations/ResourceAllocation"
            ):
                bad_ra = False
                allocation = {}

                #
                # Get ResourceAllocation elements and verify they're nonempty
                #
                type_ = safe_element_text(eResourceAllocation.find("./Type"))
                eExecuteResourceGroup_list = eResourceAllocation.findall(
                    "./ExecuteResourceGroups/ExecuteResourceGroup"
                )
                eSubmitResource_list = eResourceAllocation.findall(
                    "./SubmitResources/SubmitResource"
                )
                for var, name in [
                    (type_, "Type"),
                    (eExecuteResourceGroup_list, "ExecuteResourceGroups"),
                    (eSubmitResource_list, "SubmitResources"),
                ]:
                    if not var:
                        log.warning(
                            "ResourceAllocation has a missing or empty %s: %s",
                            name,
                            elem2str(eResourceAllocation),
                        )
                        bad_ra = True

                if bad_ra:
                    continue

                allocation["type"] = type_

                #
                # Transform the list of SubmitResource elements
                #
                allocation["submit_resources"] = []
                for eSubmitResource in eSubmitResource_list:
                    resinfo = self.resinfo_by_name.get(
                        safe_element_text(eSubmitResource)
                    )
                    if not resinfo:
                        log.warning(
                            "Skipping missing or malformed SubmitResource: %s",
                            elem2str(eSubmitResource),
                        )
                        continue

                    allocation["submit_resources"].append(
                        {
                            "fqdn": resinfo.fqdn,
                            "group_name": resinfo.group_name,
                            "name": resinfo.name,
                        }
                    )

                #
                # Transform the list of ExecuteResourceGroup elements
                #
                allocation["execute_resource_groups"] = []
                for eExecuteResourceGroup in eExecuteResourceGroup_list:
                    group_name = safe_element_text(
                        eExecuteResourceGroup.find("./GroupName")
                    )
                    local_allocation_id = safe_element_text(
                        eExecuteResourceGroup.find("./LocalAllocationID")
                    )
                    if not group_name or not local_allocation_id:
                        log.warning(
                            "Skipping malformed ExecuteResourceGroup: %s",
                            elem2str(eExecuteResourceGroup),
                        )
                        continue

                    resinfo_list = self.grouped_resinfo.get(group_name)
                    if not resinfo_list:
                        log.warning(
                            "Skipping missing or empty ExecuteResourceGroup %s",
                            group_name,
                        )
                        continue

                    ces = [
                        {"fqdn": x.fqdn, "name": x.name}
                        for x in resinfo_list
                        if x.is_ce()
                    ]
                    allocation["execute_resource_groups"].append(
                        {
                            "ces": ces,
                            "group_name": group_name,
                            "local_allocation_id": local_allocation_id,
                        }
                    )

                # Done with this allocation
                allocations.append(allocation)

        # Done with all projects
        return ret

    #
    #
    # Internal
    #
    #

    def _get_data(self, endpoint: str) -> ET.Element:
        """Download XML topology data from from `endpoint` and parse it as an ET.Element.
        `endpoint` is a path under the topology host, e.g. "/miscproject/xml".

        Returns the parsed data.

        """
        try:
            with urlopen(self.topology_base + endpoint) as response:
                xml_bytes = response.read()  # type: bytes
        except EnvironmentError as err:
            raise DataError("Topology query to %s failed" % endpoint) from err

        if not xml_bytes:
            raise DataError("Topology query to %s returned no data" % endpoint)

        try:
            element = ET.fromstring(xml_bytes)
        except (ET.ParseError, UnicodeDecodeError) as err:
            raise DataError(
                "Topology query to %s couldn't be parsed" % endpoint
            ) from err

        return element


def safe_element_text(element: Optional[ET.Element]) -> str:
    return getattr(element, "text", "").strip()


def findall_nonempty(elt, path):
    return list(filter(None, map(safe_element_text, elt.findall(path))))


def elem2str(element: ET.Element) -> str:
    return ET.tostring(element, encoding="unicode")
    # ^^ 'encoding="unicode"' tells ET.tostring() to return an str not a bytes


def between(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def main(argv):
    parser = ArgumentParser(description=__doc__, prog=argv[0])
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Say more; can specify twice.",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="count",
        default=0,
        help="Say less; can specify twice.",
    )
    parser.add_argument(
        "--outdir",
        metavar="DIR",
        default="/run/topology-cache",
        help="Directory to write topology files to. [%(default)s]",
    )
    parser.add_argument(
        "--topology",
        metavar="URL",
        default=TOPOLOGY,
        help="Base URL of the Topology service. [%(default)s]",
    )

    args = parser.parse_args(argv[1:])
    log.setLevel(
        between(logging.WARNING + (10 * (args.quiet - args.verbose)), logging.DEBUG, logging.CRITICAL)
    )

    try:
        os.makedirs(args.outdir, exist_ok=True)
    except OSError as e:
        pass  # ¯\_(ツ)_/¯

    data = TopologyData(args.topology)

    # Save the raw data
    path = ""
    try:
        for filename, contents in [
            ("miscproject.xml", data.projects),
            ("rgsummary.xml", data.resources),
        ]:
            path = os.path.join(args.outdir, filename)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(elem2str(contents))
                log.info("Wrote %s", path)
    except OSError as e:
        return f"Couldn't write {path}: {str(e)}"

    # Compose and save the json files
    project_resource_allocations = data.get_project_resource_allocations()
    resource_info_lookups = data.get_resource_info_lookups()
    for filename, contents in [
        ("project_resource_allocations.json", project_resource_allocations),
        ("resource_info_lookups.json", resource_info_lookups),
    ]:
        path = os.path.join(args.outdir, filename)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    contents,
                    fh,
                    skipkeys=True,
                    indent=2,
                    sort_keys=True,
                )
                log.info("Wrote %s", path)
        except OSError as e:
            return f"Couldn't write {path}: {str(e)}"

    return 0


if __name__ == "__main__":
    logging.basicConfig(format="%(message)s")
    sys.exit(main(sys.argv))
