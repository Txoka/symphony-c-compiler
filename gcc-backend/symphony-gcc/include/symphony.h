#ifndef SYMPHONY_DEVICE_H
#define SYMPHONY_DEVICE_H

unsigned int input(void);
void output(unsigned int value);
unsigned int keyboard(void);
void screen(unsigned int address, unsigned int length);
unsigned int time(void);
unsigned int time_low(void);
unsigned int time_high(void);
unsigned int persistent_load(unsigned int address);
void persistent_store(unsigned int address, unsigned int value);
void jump(unsigned int address);
unsigned int symphony_heap_remaining(void *pointer);

#endif
