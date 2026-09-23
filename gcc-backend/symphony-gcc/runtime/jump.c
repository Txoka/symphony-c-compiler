/* Transfer to a freshly loaded program image.  Kept separate from the common
   runtime so ordinary programs do not pay for this self-host-only helper. */
void jump(unsigned int address) {
    __asm__ volatile ("jmp\t%0" : : "r"(address));
    __builtin_unreachable();
}
