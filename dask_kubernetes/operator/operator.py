import asyncio
import threading

from distributed.core import rpc

import kopf
import kubernetes_asyncio as kubernetes

from uuid import uuid4

from dask_kubernetes.auth import ClusterAuth
from dask_kubernetes.utils import (
    get_scheduler_address,
)


lock = threading.Lock()


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super(Singleton, cls).__call__(
                        *args, **kwargs
                    )
        return cls._instances[cls]


class DaskRPC(metaclass=Singleton):
    def __init__(self, address):
        self.scheduler_comm = rpc(address)


def build_scheduler_pod_spec(name, image, env):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"{name}-scheduler",
            "labels": {
                "dask.org/cluster-name": name,
                "dask.org/component": "scheduler",
            },
        },
        "spec": {
            "containers": [
                {
                    "name": "scheduler",
                    "image": image,
                    "args": ["dask-scheduler"],
                    "ports": [
                        {
                            "name": "comm",
                            "containerPort": 8786,
                            "protocol": "TCP",
                        },
                        {
                            "name": "dashboard",
                            "containerPort": 8787,
                            "protocol": "TCP",
                        },
                    ],
                    "readinessProbe": {
                        "tcpSocket": {"port": "comm"},
                        "initialDelaySeconds": 5,
                        "periodSeconds": 10,
                    },
                    "livenessProbe": {
                        "tcpSocket": {"port": "comm"},
                        "initialDelaySeconds": 15,
                        "periodSeconds": 20,
                    },
                    "env": env,
                }
            ]
        },
    }


def build_scheduler_service_spec(name):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "labels": {
                "dask.org/cluster-name": name,
            },
        },
        "spec": {
            "selector": {
                "dask.org/cluster-name": name,
                "dask.org/component": "scheduler",
            },
            "ports": [
                {
                    "name": "comm",
                    "protocol": "TCP",
                    "port": 8786,
                    "targetPort": 8786,
                },
                {
                    "name": "dashboard",
                    "protocol": "TCP",
                    "port": 8787,
                    "targetPort": 8787,
                },
            ],
        },
    }


def build_worker_pod_spec(name, namespace, image, uuid, scheduler_name, env):
    worker_name = f"{name}-worker-{uuid}"
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": worker_name,
            "labels": {
                "dask.org/cluster-name": scheduler_name,
                "dask.org/workergroup-name": name,
                "dask.org/component": "worker",
            },
        },
        "spec": {
            "containers": [
                {
                    "name": "scheduler",
                    "image": image,
                    "args": [
                        "dask-worker",
                        f"tcp://{scheduler_name}.{namespace}:8786",
                        f"--name={worker_name}",
                    ],
                    "env": env,
                }
            ]
        },
    }


def build_worker_group_spec(name, cluster, image, replicas, resources, env):
    return {
        "apiVersion": "kubernetes.dask.org/v1",
        "kind": "DaskWorkerGroup",
        "metadata": {"name": f"{name}-worker-group"},
        "spec": {
            "cluster": cluster,
            "imagePullSecrets": None,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "replicas": replicas,
            "resources": resources,
            "env": env,
        },
    }


def build_cluster_spec(name, image, replicas, resources, env):
    return {
        "apiVersion": "kubernetes.dask.org/v1",
        "kind": "DaskCluster",
        "metadata": {"name": f"{name}-cluster"},
        "spec": {
            "imagePullSecrets": None,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "protocol": "tcp",
            "scheduler": {
                "resources": resources,
                "env": env,
                "serviceType": "ClusterIP",
            },
            "replicas": replicas,
            "resources": resources,
            "env": env,
        },
    }


async def wait_for_service(api, service_name, namespace):
    """Block until service is available."""
    while True:
        try:
            await api.read_namespaced_service(service_name, namespace)
            break
        except Exception:
            asyncio.sleep(0.1)


@kopf.on.startup()
async def startup(**kwargs):
    await ClusterAuth.load_first()


@kopf.on.create("daskcluster")
async def daskcluster_create(spec, name, namespace, logger, **kwargs):
    logger.info(
        f"A DaskCluster has been created called {name} in {namespace} with the following config: {spec}"
    )
    async with kubernetes.client.api_client.ApiClient() as api_client:
        api = kubernetes.client.CoreV1Api(api_client)

        # TODO Check for existing scheduler pod
        scheduler_spec = spec.get("scheduler", {})
        data = build_scheduler_pod_spec(
            name, spec.get("image"), scheduler_spec.get("env", [])
        )
        kopf.adopt(data)
        await api.create_namespaced_pod(
            namespace=namespace,
            body=data,
        )
        # await wait_for_scheduler(name, namespace)
        logger.info(
            f"A scheduler pod has been created called {data['metadata']['name']} in {namespace} \
            with the following config: {data['spec']}"
        )

        # TODO Check for existing scheduler service
        data = build_scheduler_service_spec(name=name)
        kopf.adopt(data)
        await api.create_namespaced_service(
            namespace=namespace,
            body=data,
        )
        await wait_for_service(api, data["metadata"]["name"], namespace)
        logger.info(
            f"A scheduler service has been created called {data['metadata']['name']} in {namespace} \
            with the following config: {data['spec']}"
        )
        data = build_worker_group_spec(
            name=f"{name}-default",
            cluster=name,
            image=spec.get("image"),
            replicas=spec.get("replicas"),
            resources=spec.get("resources"),
            env=spec.get("env"),
        )
        api = kubernetes.client.CustomObjectsApi(api_client)
        await api.create_namespaced_custom_object(
            group="kubernetes.dask.org",
            version="v1",
            plural="daskworkergroups",
            namespace=namespace,
            body=data,
        )
        logger.info(
            f"A worker group has been created called {data['metadata']['name']} in {namespace} \
            with the following config: {data['spec']}"
        )


@kopf.on.create("daskworkergroup")
async def daskworkergroup_create(spec, name, namespace, logger, **kwargs):
    async with kubernetes.client.api_client.ApiClient() as api_client:
        api = kubernetes.client.CustomObjectsApi(api_client)
        cluster = await api.get_namespaced_custom_object(
            group="kubernetes.dask.org",
            version="v1",
            plural="daskclusters",
            namespace=namespace,
            name=spec["cluster"],
        )
        new_spec = dict(spec)
        kopf.adopt(new_spec, owner=cluster)
        api.api_client.set_default_header(
            "content-type", "application/merge-patch+json"
        )
        await api.patch_namespaced_custom_object(
            group="kubernetes.dask.org",
            version="v1",
            plural="daskworkergroups",
            namespace=namespace,
            name=name,
            body=new_spec,
        )
        logger.info(f"Successfully adopted by {spec['cluster']}")
    await daskworkergroup_update(
        spec=spec, name=name, namespace=namespace, logger=logger, **kwargs
    )


@kopf.on.update("daskworkergroup")
async def daskworkergroup_update(spec, name, namespace, logger, **kwargs):
    async with kubernetes.client.api_client.ApiClient() as api_client:
        api = kubernetes.client.CoreV1Api(api_client)
        workers = await api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"dask.org/workergroup-name={name}",
        )
        current_workers = len(workers.items)
        desired_workers = spec["replicas"]
        workers_needed = desired_workers - current_workers
        if workers_needed > 0:
            for _ in range(workers_needed):
                data = build_worker_pod_spec(
                    name=name,
                    namespace=namespace,
                    image=spec.get("image"),
                    uuid=uuid4().hex,
                    scheduler_name=spec.get("cluster"),
                    env=spec.get("env", []),
                )
                kopf.adopt(data)
                await api.create_namespaced_pod(
                    namespace=namespace,
                    body=data,
                )
            logger.info(f"Scaled worker group {name} up to {spec['replicas']} workers.")
        if workers_needed < 0:
            service_name = f"{name.split('-')[0]}-cluster"
            address = await get_scheduler_address(service_name, namespace)
            scheduler = DaskRPC(address=address).scheduler_comm
            worker_ids = await scheduler.workers_to_close(
                n=-workers_needed, attribute="name"
            )
            # TODO: Check that were deting workers in the right worker group
            logger.info(f"Workers to close: {worker_ids}")
            for wid in worker_ids:
                await api.delete_namespaced_pod(
                    name=wid,
                    namespace=namespace,
                )
            logger.info(
                f"Scaled worker group {name} down to {spec['replicas']} workers."
            )


@kopf.on.delete("daskcluster")
async def daskcluster_delete(spec, name, namespace, logger, **kwargs):
    address = await get_scheduler_address(name, namespace)
    scheduler = DaskRPC(address=address).scheduler_comm
    scheduler.close_comms()
    scheduler.close_rpc()
