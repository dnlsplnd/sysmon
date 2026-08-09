"""The navigable pages of the monitor, in sidebar order."""

from .base import Page
from .cpu import CpuPage
from .disk import DiskPage
from .gpu import GpuPage
from .memory import MemoryPage
from .network import NetworkPage
from .overview import OverviewPage
from .processes import ProcessesPage
from .sensors import SensorsPage

PAGE_CLASSES = [
    OverviewPage,
    CpuPage,
    MemoryPage,
    GpuPage,
    DiskPage,
    NetworkPage,
    ProcessesPage,
    SensorsPage,
]

__all__ = [
    "Page",
    "PAGE_CLASSES",
    "OverviewPage",
    "CpuPage",
    "MemoryPage",
    "GpuPage",
    "DiskPage",
    "NetworkPage",
    "ProcessesPage",
    "SensorsPage",
]
