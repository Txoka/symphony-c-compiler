/* Software 32-bit multiply for Symphony/Dynphony.

   This ISA has no hardware multiply instruction (docs/isa.txt), and
   unlike __divsi3/__udivsi3/__modsi3/__umodsi3 (which come from libgcc's
   own always-available divmod.c/udivmod.c, wired in via LIB2ADD in
   t-symphony), __mulsi3 has NO portable equivalent anywhere in generic
   libgcc -- every target either has a hardware multiply instruction (and
   a "mulsi3" expander in its own .md), or provides its own soft-multiply
   source file wired in via LIB2ADD, exactly as done here. This mirrors
   libgcc/config/iq2000/lib2funcs.c (iq2000 is another no-hardware-
   multiply target using the same divmod.c/udivmod.c/udivmodsi4.c +
   lib2funcs.c LIB2ADD combination) -- same shift-and-add algorithm,
   ported to this target's naming.  */

typedef unsigned int USItype __attribute__ ((mode (SI)));

USItype
__mulsi3 (USItype a, USItype b)
{
  USItype c = 0;

  while (a != 0)
    {
      if (a & 1)
	c += b;
      a >>= 1;
      b <<= 1;
    }

  return c;
}
