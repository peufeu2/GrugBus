#!/usr/bin/python
# -*- coding: utf-8 -*-

class Interp:
    def __init__( self, *xylist, var=None ):
        # argument is a single value: return constant
        if len(xylist)==1 and isinstance( xylist[0], (int, float) ):
            self.xylist = ( (0, xylist[0]), )
        else:
            assert len(xylist)
            self.xylist = tuple(sorted( xylist ))
            for xa, xb in zip( self.xylist[1:], self.xylist[:-1 ]):
                assert xa != xb # can't have duplicate X values in list
        self.var = var

    def __call__( self, x ):
        if self.var:
            x = getattr( x, self.var )
        xylist = self.xylist
        
        if x <= xylist[0][0]:
            return xylist[0][1]

        if x >= xylist[-1][0]:
            return xylist[-1][1]

        for pos, (xa,ya) in enumerate( xylist ):
            if x >= xa:
                xb,yb = xylist[pos+1]
                return ya + (yb-ya)*(x-xa)/(xb-xa)



if __name__ == "__main__":
    class Ctx:
        pass

    f = Interp( (0,1), (10,100), (20,110) )
    for x in range( -10, 30 ):
        print( x, f(x) )

    f = Interp( 10  )
    for x in range( -10, 30 ):
        print( x, f(x) )

    ctx = Ctx()
    f = Interp( (0,1), (10,100), (20,110), var="soc" )
    for x in range( -10, 30 ):
        ctx.soc = x
        print( x, f(ctx) )


