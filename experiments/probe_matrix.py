import cairocffi as cairo

# cairo.Matrix(xx, yx, xy, yy, x0, y0)
T = cairo.Matrix(1, 0, 0, 1, 10, 20)   # translate (10,20)
R = cairo.Matrix.init_rotate(1.5707963267948966)  # rotate 90deg

# What does A.multiply(B) do? cairo docs: result = A * B
M = R.multiply(T)
p = M.transform_point(1, 0)
print("R.multiply(T) on (1,0) =", p, "(expect T first then R: R*(1+10,0+20)=(-20,11))")

M2 = T.multiply(R)
p2 = M2.transform_point(1, 0)
print("T.multiply(R) on (1,0) =", p2, "(expect R first then T: (10,21))")

# How does context.transform(M) combine with existing CTM?
surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 100, 100)
ctx = cairo.Context(surf)
ctx.translate(5, 6)          # CTM = T1
ctx.transform(R)             # per cairo docs: CTM = CTM x R? or R x CTM?
m = ctx.get_matrix()
print("ctx after translate(5,6); transform(R):", m)
p3 = m.transform_point(1, 0)
print("maps (1,0) ->", p3)

# matrix inversion sanity: applying invert twice
A = cairo.Matrix(1, 0, 0, 1, 3, 4)
A.invert()
print("inverted translate matrix:", A)
A.invert()
print("inverted again:", A)
