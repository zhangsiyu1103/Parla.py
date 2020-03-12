import numpy as np

from parla import Parla
from parla.cpu import cpu
from parla.tasks import spawn
from numba import jit

# Divide an array of values into two "bins"
# and return the index separating them
@jit
def subdivide(array, split):
    low = 0
    high = array.shape[0] - 1
    while True:
        while array[low] <= split:
            low += 1
        if low > high:
            return low
        while array[high] > split:
            if low >= high:
                return low
            high -= 1
        array[low], array[high] = array[high], array[low]
        low += 1
        if low >= high:
            return low
        high -= 1

# Insertion sort for non-recursive base case
@jit
def insertion_sort(array):
    for i in range(array.shape[0]):
        j = i
        while j > 0 and array[j-1] > array[j]:
            array[j-1], array[j] = array[j], array[j-1]
            j -= 1

def quicksort(array, lower = 0, upper = 1., threshold = 100):
    if array.shape[0] < threshold:
        insertion_sort(array)
        return
    split = .5 * (lower + upper)
    split_idx = subdivide(array, split)
    lower_array = array[:split_idx]
    upper_array = array[split_idx:]
    @spawn(placement = cpu)
    def lower_block_task():
        quicksort(lower_array, lower, split, threshold)
    @spawn(placement = cpu)
    def upper_block_task():
        quicksort(upper_array, split, upper, threshold)

a = np.random.rand(10000)
a_sorted = np.sort(a)
with Parla():
    quicksort(a)
assert (a == a_sorted).all()