/* Reuses one expensive expression whose operands are unchanged. */
#include <symphony.h>

int main(void) {
    unsigned int value = input();
    return value * value + value * value;
}
