# Copyright (c) 2021 SUSE LLC
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of version 3 of the GNU General Public License as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.   See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, contact SUSE LLC.
#
# To contact SUSE about this file by physical or electronic mail,
# you may find current contact information at www.suse.com

import re
import os
import warnings
from time import sleep
from datetime import datetime, timedelta

import pytest


pytest_plugins = [
    'harvester_e2e_tests.fixtures.api_client',
    'harvester_e2e_tests.fixtures.rancher_api_client',
]


@pytest.fixture(scope="session")
def rancher_wait_timeout(request):
    return request.config.getoption("--rancher-cluster-wait-timeout")


@pytest.fixture(scope='class')
def rke1_cluster_name(unique_name):
    return f"{unique_name}-rke1"


@pytest.fixture(scope='class')
def rke2_cluster_name(unique_name):
    return f"{unique_name}-rke2"


@pytest.fixture(scope='module')
def rke1_k8s_version(request, k8s_version, rancher_api_client):
    configured = request.config.getoption("--RKE1-version")
    if configured:
        return configured

    # `v1.24.11+rke2r1` -> `v1.24.11-rancher2-1`
    version = re.sub(r'\+rke(\d+)r(\d+)', lambda g: "-rancher%s-%s" % g.groups(), k8s_version)

    code, data = rancher_api_client.settings.get('k8s-versions-current')
    assert 200 == code, (code, data)
    current = data['value']
    if version in current:
        return version

    code, data = rancher_api_client.settings.get('k8s-versions-deprecated')
    assert 200 == code, (code, data)
    if data['value'] and version in data['value']:
        return version

    latest = current.split(',')[-1]

    warnings.warn(UserWarning(
        f"Kubernetes version {version} is not in supported list,"
        f" change to use latest version {latest} instead."
    ))

    return latest


@pytest.fixture(scope='session')
def vlan_network(request, api_client):
    vlan_nic = request.config.getoption('--vlan-nic')
    vlan_id = request.config.getoption('--vlan-id')
    assert -1 != vlan_id, "Rancher integration test needs VLAN"

    api_client.clusternetworks.create(vlan_nic)
    api_client.clusternetworks.create_config(vlan_nic, vlan_nic, vlan_nic)

    network_name = f'vlan-network-{vlan_id}'
    code, data = api_client.networks.get(network_name)
    if code != 200:
        code, data = api_client.networks.create(network_name, vlan_id, cluster_network=vlan_nic)
        assert 201 == code, (
            f"Failed to create network-attachment-definition {network_name} \
                with error {code}, {data}"
        )

    data['id'] = data['metadata']['name']
    yield data

    api_client.networks.delete(network_name)


@pytest.fixture(scope="session")
def focal_image_url(request):
    external = 'https://cloud-images.ubuntu.com/focal/current/'
    base_url = request.config.getoption('--image-cache-url') or external
    return os.path.join(base_url, "focal-server-cloudimg-amd64.img")


@pytest.fixture(scope="class")
def focal_image(api_client, unique_name, focal_image_url, wait_timeout):
    code, data = api_client.images.create_by_url(unique_name, focal_image_url)
    assert 201 == code, (
        f"Failed to upload focal image with error: {code}, {data}"
    )

    endtime = datetime.now() + timedelta(seconds=wait_timeout)
    while endtime > datetime.now():
        code, data = api_client.images.get(unique_name)
        if 'status' in data and 'progress' in data['status'] and \
                data['status']['progress'] == 100:
            break
        sleep(5)
    else:
        raise AssertionError(
            f"Image {unique_name} can't be ready with {wait_timeout} timed out\n"
            f"Got error: {code}, {data}"
        )

    namespace = data['metadata']['namespace']
    name = data['metadata']['name']

    yield dict(ssh_user="ubuntu", id=f"{namespace}/{name}")

    api_client.images.delete(name, namespace)


@pytest.fixture(scope='module')
def harvester_mgmt_cluster(api_client, rancher_api_client, unique_name, wait_timeout):
    cluster_name = f"{unique_name}-harv"

    code, data = rancher_api_client.mgmt_clusters.create_harvester(cluster_name)
    assert 201 == code, (code, data)

    endtime = datetime.now() + timedelta(seconds=wait_timeout)
    while endtime > datetime.now():
        code, data = rancher_api_client.mgmt_clusters.get(cluster_name)
        if data.get('status', {}).get('clusterName'):
            break
        sleep(5)
    else:
        raise AssertionError(
            f"Can't find clusterName in MgmtCluster {cluster_name} with {wait_timeout} timed out\n"
            f"Got error: {code}, {data}"
        )

    yield dict(id=data['status']['clusterName'], name=cluster_name)

    # teardown
    rancher_api_client.mgmt_clusters.delete(cluster_name)
    updates = {
        "value": ""
    }
    api_client.settings.update("cluster-registration-url", updates)


@pytest.fixture(scope='class')
def harvester_cloud_credential(api_client, rancher_api_client,
                               harvester_mgmt_cluster, unique_name):
    harvester_kubeconfig = api_client.generate_kubeconfig()
    code, data = rancher_api_client.cloud_credentials.create(
        unique_name,
        harvester_kubeconfig,
        harvester_mgmt_cluster['id']
    )

    assert 201 == code, (
        f"Failed to create cloud credential with error: {code}, {data}"
    )

    code, data = rancher_api_client.cloud_credentials.get(data['id'])
    assert 200 == code, (
        f"Failed to get cloud credential {data['id']} with error: {code}, {data}"
    )

    yield data

    rancher_api_client.cloud_credentials.delete(data['id'])


@pytest.mark.p0
@pytest.mark.rancher
@pytest.mark.dependency(name="import_harvester")
def test_import_harvester(api_client, rancher_api_client, wait_timeout,
                          harvester_mgmt_cluster):
    cluster_id, cluster_name = harvester_mgmt_cluster['id'], harvester_mgmt_cluster['name']

    endtime = datetime.now() + timedelta(seconds=wait_timeout)
    while endtime > datetime.now():
        code, data = rancher_api_client.cluster_registration_tokens.get(cluster_id)
        if 200 == code and data.get('manifestUrl'):
            break
        sleep(5)
    else:
        raise AssertionError(
            f"Harvester Imported but registration URL is NOT available after {wait_timeout}s\n"
            f"Cluster Info: {harvester_mgmt_cluster}\n"
            f"API Status({code}): {data}"
        )

    updates = dict(value=data['manifestUrl'])
    code, data = api_client.settings.update("cluster-registration-url", updates)
    assert 200 == code, (
        f"Failed to update Harvester's settings `cluster-registration-url`"
        f" with error: {code}, {data}"
    )

    # Check Cluster becomes `active` in Rancher's Virtualization Management
    endtime = datetime.now() + timedelta(seconds=wait_timeout)
    while endtime > datetime.now():
        code, data = rancher_api_client.mgmt_clusters.get(cluster_name)
        state = data['metadata']['state']
        if "active" == state['name'] and "Ready" in state['message']:
            break
        sleep(5)
    else:
        raise AssertionError(
            f"Imported Harvester stuck in state {state['name']!r} after {wait_timeout}s\n"
            f"API Status({code}): {data}"
        )


@pytest.mark.p1
@pytest.mark.rancher
@pytest.mark.dependency(depends=["import_harvester"])
def test_add_project_owner_user(api_client, rancher_api_client, unique_name, wait_timeout,
                                harvester_mgmt_cluster):
    cluster_id = harvester_mgmt_cluster['id']
    username, password = f"user-{unique_name}", unique_name

    spec = rancher_api_client.users.Spec(password)
    # create user
    code, data = rancher_api_client.users.create(username, spec)
    assert 201 == code, (
        f"Failed to create user {username!r}\n"
        f"API Status({code}): {data}"
    )
    uid, upids = data['id'], data['principalIds']

    # add role `user` to user
    code, data = rancher_api_client.users.add_role(uid, 'user')
    assert 201 == code, (
        f"Failed to add role 'user' for user {username!r}\n"
        f"API Status({code}): {data}"
    )

    # Get `Default` project's uid
    cluster_api = rancher_api_client.clusters.explore(cluster_id)
    code, data = cluster_api.projects.get_by_name('Default')
    assert 200 == code, (code, data)
    project_id = data['id']
    # add user to `Default` project as *project-owner*
    code, data = cluster_api.project_members.create(project_id, upids[0], "project-owner")
    assert 201 == code, (code, data)
    proj_muid = data['id']

    # Login as the user
    endpoint = rancher_api_client.endpoint
    user_rapi = rancher_api_client.login(endpoint, username, password, ssl_verify=False)
    user_capi = user_rapi.clusters.explore(cluster_id)
    # Check user can only view the project he joined
    code, data = user_capi.projects.get()
    assert 200 == code, (code, data)
    assert 1 == len(data['data']), (code, data)

    # teardown
    cluster_api.project_members.delete(proj_muid)
    rancher_api_client.users.delete(uid)


@pytest.mark.p0
@pytest.mark.rancher
class TestRKE:
    @pytest.mark.rke2
    @pytest.mark.dependency(depends=["import_harvester"])
    def test_create_rke2(self, rancher_api_client, unique_name, harvester_mgmt_cluster,
                         harvester_cloud_credential, rke2_cluster_name, focal_image, vlan_network,
                         k8s_version, rancher_wait_timeout):
        cluster_name = harvester_mgmt_cluster['id']
        code, data = rancher_api_client.kube_configs.create(
            rke2_cluster_name,
            cluster_name
        )
        assert 200 == code, (
            f"Failed to create harvester kubeconfig for rke2 with error: {code}, {data}"
        )
        assert "" != data, (
            f"Harvester kubeconfig for rke2 should not be empty: {code}, {data}"
        )

        kubeconfig = data

        code, data = rancher_api_client.secrets.create(
            name=unique_name,
            data={
                "credential": kubeconfig[1:-1].replace("\\n", "\n")
            },
            annotations={
                "v2prov-secret-authorized-for-cluster": rke2_cluster_name,
                "v2prov-authorized-secret-deletes-on-cluster-removal": "true"
            }
        )
        assert 201 == code, (
            f"Failed to create secret with error: {code}, {data}"
        )

        cloud_provider_config_id = f"{data['metadata']['namespace']}:{data['metadata']['name']}"

        code, data = rancher_api_client.harvester_configs.create(
            name=unique_name,
            cpus="2",
            mems="4",
            disks="40",
            image_id=focal_image['id'],
            network_id=vlan_network['id'],
            ssh_user=focal_image['ssh_user'],
            user_data=(
                "#cloud-config\n"
                "password: test\n"
                "chpasswd:\n"
                "    expire: false\n"
                "ssh_pwauth: true\n"
            ),
        )
        assert 201 == code, (
            f"Failed to create harvester config with error: {code}, {data}"
        )

        code, data = rancher_api_client.mgmt_clusters.create(
            name=rke2_cluster_name,
            cloud_provider_config_id=cloud_provider_config_id,
            hostname_prefix=f"{rke2_cluster_name}-",
            harvester_config_name=unique_name,
            k8s_version=k8s_version,
            cloud_credential_id=harvester_cloud_credential['id']
        )
        assert 201 == code, (
            f"Failed to create RKE2 MgmtCluster {unique_name} with error: {code}, {data}"
        )

        endtime = datetime.now() + timedelta(seconds=rancher_wait_timeout)
        while endtime > datetime.now():
            code, data = rancher_api_client.mgmt_clusters.get(rke2_cluster_name)
            if data.get('status', {}).get('ready', False):
                break
            sleep(5)
        else:
            raise AssertionError(
                f"RKE2 MgmtCluster {rke2_cluster_name} can't be ready \
                    with {rancher_wait_timeout} timed out\n"
                f"Got error: {code}, {data}"
            )

    @pytest.mark.rke2
    @pytest.mark.dependency(depends=["TestRKE::test_create_rke2"])
    def test_create_pvc(self, rancher_api_client, harvester_mgmt_cluster,
                        unique_name, wait_timeout):
        cluster_id = harvester_mgmt_cluster['id']
        capi = rancher_api_client.clusters.explore(cluster_id)
        # Create PVC
        size = "1Gi"
        spec = capi.pvcs.Spec(size)
        code, data = capi.pvcs.create(unique_name, spec)
        assert 201 == code, (code, data)
        # Verify PVC is created
        endtime = datetime.now() + timedelta(seconds=wait_timeout)
        while endtime > datetime.now():
            code, data = capi.pvcs.get(unique_name)
            if "Bound" == data['status'].get('phase'):
                break
            sleep(5)
        else:
            raise AssertionError(
                f"PVC Created but stuck in phase {data['status'].get('phase')}\n"
                f"Status({code}): {data}"
            )
        # Verify the PV for created PVC
        pv_code, pv_data = capi.pvs.get(data['spec']['volumeName'])
        assert 200 == pv_code, (
            f"Relevant PV is NOT available for created PVC's PV({data['spec']['volumeName']})\n"
            f"Response data of PV: {data}"
        )
        # Verify size of the PV is aligned to requested size of PVC
        assert size == pv_data['spec']['capacity']['storage'], (
            "Size of the PV is NOT aligned to requested size of PVC,"
            f" expected: {size}, PV's size: {pv_data['spec']['capacity']['storage']}\n"
            f"Response data of PV: {data}"
        )
        # Verify PVC's size
        created_spec = capi.pvcs.Spec.from_dict(data)
        assert size == spec.size, (
            f"Size is NOT correct in created PVC, expected: {size}, created: {spec.size}\n"
            f"Response data: {data}"
        )
        # Verify the storage class exists
        sc_code, sc_data = capi.scs.get(created_spec.storage_cls)
        assert 200 == sc_code, (
            f"Storage Class is NOT exists for created PVC\n"
            f"Created PVC Spec: {data}\n"
            f"SC Status({sc_code}): {sc_data}"
        )
        # verify the storage class is marked `default`
        assert 'true' == sc_data['metadata']['annotations'][capi.scs.DEFAULT_KEY], (
            f"Storage Class is NOT the DEFAULT for created PVC\n"
            f"Requested Storage Class: {spec.storage_cls!r}"
            f"Created PVC Spec: {data}\n"
            f"SC Status({sc_code}): {sc_data}"
        )

        # teardown
        capi.pvcs.delete(unique_name)

    @pytest.mark.rke2
    @pytest.mark.dependency(depends=["TestRKE::test_create_rke2"])
    def test_delete_rke2(self, api_client, rancher_api_client, rke2_cluster_name,
                         rancher_wait_timeout):
        code, data = rancher_api_client.mgmt_clusters.delete(rke2_cluster_name)
        assert 200 == code, (
            f"Failed to delete RKE2 MgmtCluster {rke2_cluster_name} with error: {code}, {data}"
        )

        endtime = datetime.now() + timedelta(seconds=rancher_wait_timeout)
        while endtime > datetime.now():
            code, data = rancher_api_client.mgmt_clusters.get(rke2_cluster_name)
            if code == 404:
                break
            sleep(5)
        else:
            raise AssertionError(
                f"RKE2 MgmtCluster {rke2_cluster_name} can't be deleted \
                    with {rancher_wait_timeout} timed out\n"
                f"Got error: {code}, {data}"
            )

        code, data = api_client.vms.get()
        remaining_vm_cnt = 0
        for d in data.get('data', []):
            vm_name = d.get('metadata', {}).get('name', "")
            if vm_name.startswith(f"{rke2_cluster_name}-"):
                remaining_vm_cnt += 1
        assert 0 == remaining_vm_cnt, (f"Still have {remaining_vm_cnt} RKE2 VMs")

    @pytest.mark.rke1
    @pytest.mark.dependency(depends=["import_harvester"])
    def test_create_rke1(self, rancher_api_client, unique_name, rancher_wait_timeout,
                         rke1_cluster_name, rke1_k8s_version, harvester_cloud_credential,
                         focal_image, vlan_network):
        code, data = rancher_api_client.node_templates.create(
            name=unique_name,
            cpus=2,
            mems=4,
            disks=40,
            image_id=focal_image['id'],
            network_id=vlan_network['id'],
            ssh_user=focal_image['ssh_user'],
            cloud_credential_id=harvester_cloud_credential['id'],
            user_data=(
                "#cloud-config\n"
                "password: test\n"
                "chpasswd:\n"
                "    expire: false\n"
                "ssh_pwauth: true\n"
            ),
        )
        assert 201 == code, (
            f"Failed to create NodeTemplate {unique_name} with error: {code}, {data}"
        )

        node_template_id = data['id']

        code, data = rancher_api_client.clusters.create(rke1_cluster_name, rke1_k8s_version)
        assert 201 == code, (
            f"Failed to create cluster {rke1_cluster_name} with error: {code}, {data}"
        )
        cluster_id = data['id']
        # check cluster created and ready for use
        endtime = datetime.now() + timedelta(seconds=rancher_wait_timeout)
        while endtime > datetime.now():
            code, data = rancher_api_client.clusters.get(cluster_id)
            types = [c['type'] for c in data['conditions']]
            if 200 == code and "RKESecretsMigrated" in types:
                break
            sleep(3)
        else:
            raise AssertionError(
                f"RKE1 cluster {rke1_cluster_name} not ready after {rancher_wait_timeout}s\n"
                f"API Status({code}): {data}"
            )

        code, data = rancher_api_client.node_pools.create(
            cluster_id=cluster_id,
            node_template_id=node_template_id,
            hostname_prefix=f"{rke1_cluster_name}-"
        )
        assert 201 == code, (
            f"Failed to create NodePools for cluster {cluster_id} with error: {code}, {data}"
        )

        endtime = datetime.now() + timedelta(seconds=rancher_wait_timeout)
        while endtime > datetime.now():
            code, data = rancher_api_client.mgmt_clusters.get(cluster_id)
            if code == 200 and data.get('status', {}).get('ready', False):
                break
            sleep(5)
        else:
            raise AssertionError(
                f"RKE1 MgmtCluster {cluster_id} can't be ready \
                    with {rancher_wait_timeout} timed out\n"
                f"Got error: {code}, {data}"
            )

    @pytest.mark.rke1
    @pytest.mark.dependency(depends=["TestRKE::test_create_rke1"])
    def test_delete_rke1(self, api_client, rancher_api_client, rke1_cluster_name,
                         rancher_wait_timeout):
        code, data = rancher_api_client.clusters.get()
        assert 200 == code, (
            f"Failed to get Cluster with error: {code}, {data}"
        )

        cluster_id = ""
        for d in data.get('data', []):
            if d.get('appliedSpec', {}).get('displayName', "") == rke1_cluster_name:
                cluster_id = d['id']
        assert "" != cluster_id, (
            f"Failed to find MgmtCluster id for {rke1_cluster_name} cluster"
        )

        code, data = rancher_api_client.mgmt_clusters.delete(cluster_id)
        assert 200 == code, (
            f"Failed to delete RKE2 MgmtCluster {cluster_id} with error: {code}, {data}"
        )

        endtime = datetime.now() + timedelta(seconds=rancher_wait_timeout)
        while endtime > datetime.now():
            code, data = rancher_api_client.clusters.get(cluster_id)
            if code == 404:
                # in RKE1, when the cluster is deleted, VMs may still in Terminating status
                code, data = api_client.vms.get()
                remaining_vm_cnt = 0
                for d in data.get('data', []):
                    vm_name = d.get('metadata', {}).get('name', "")
                    if vm_name.startswith(f"{rke1_cluster_name}-"):
                        remaining_vm_cnt += 1
                if remaining_vm_cnt == 0:
                    break
            sleep(5)
        else:
            raise AssertionError(
                f"RKE1 cluster {cluster_id} can't be deleted \
                    with {rancher_wait_timeout} timed out\n"
                f"Got error: {code}, {data}"
            )
