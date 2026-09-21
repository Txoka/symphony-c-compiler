/* The loop-pressure inliner guard keeps helper out of this outer loop. */
int helper(int value) {
    int result = 0;
    while (value) {
        result += value & 1;
        value >>= 1;
    }
    return result;
}

int main(void) {
    int total = 0;
    int value = 1;
    while (value < 100) {
        total += helper(value);
        value += 1;
    }
    return total;
}
