# -*- coding: utf-8 -*-
"""
Created on Wed May 13 22:36:13 2020

@author: Alexis Brandeker, alexis@astro.su.se

Module with the star_bg class and methods to use the catalog file of background
stars (retrieved from Gaia) to produce synthetic images of the field of view
observed by CHEOPS, using an empirical PSF.
"""
from functools import partial
import numpy as np
from astropy.io import fits
from .psf_model import psf_model
from .read import PSFs as load_PSFs
from .spline_pca import SplinePCA
from .psf import fit as psf_fit


class WorkCat:
    """Data class for processed catalog data for a single frame that are
    relevantfor producing a star background.
    """
    def __init__(self,
                 x, y,
                 fscale,
                 dxs, dys):
        self.catsize = len(x)
        self.x = x                          # Detector coordinate in frame, one entry per star
        self.y = y                          # (roll rotated, target jitter offset)
        self.fscale = fscale                # Flux relative to target, one entry per star
        self.dxs = dxs  # List of arrays, containing offsets of PSFs per star
        self.dys = dys  # List of arrays, containing offsets of PSFs per star
        self.coeff = self.catsize*[None]    # List of coefficients for composite PSF


class star_bg:
    """Reads catalogue data on background stars and produces
    images of stars, to be removed from observations. Adjusts PSFs with
    rotational blurring and computes smearing trails.
    """
    def __init__(self, starcatfile, psf_lib, maxrad=None,
                 fscalemin=1e-5, pixel_scale=1.01):
        self.pxl_scl = pixel_scale    # CHEOPS pixel scale, arcsec/pixel
        self.psf_mod_Teff = np.array([3000.0, 4000.0, 5000.0, 6000.0, 8000.0, 10000.0])
        self.default_psf_id = 2
        self.psf_lib = psf_lib
        self.xpos, self.ypos, self.fscale, self.Teff, self.gaiaID = \
            self.read_starcat(starcatfile, maxrad=maxrad, fscalemin=fscalemin)
        self.catsize = len(self.fscale)
        self.psf_ids, self.psfs = self.assign_psf()


    def assign_psf(self):
        """This method produces a PSF from the library corresponding to each
        of the temperatures in the limited list psf_mod_Teff. An index to
        this list is then estimated for each catalogued background star, using 
        the nearest match to the catalogued temperature (and a default index
        if no temperature is available).
        """
        psf_ids = self.default_psf_id * np.ones(self.catsize, dtype='int')
        psfs = []

        for Teff in self.psf_mod_Teff:
            psf_files = self.psf_lib.best_Teff_matches(Teff, min_num=10)
            psf_list = load_PSFs(psf_files, self.psf_lib.psf_ref_path)
            spca = SplinePCA(psf_list, num_eigen=1)
            psfs.append(psf_model(spca.get_median_spline()))
        for n in range(self.catsize):
            if np.isfinite(self.Teff[n]):
                psf_ids[n] = np.absolute(self.psf_mod_Teff-self.Teff[n]).argmin()
        return psf_ids, psfs


    def read_starcat(self, starcatfile, maxrad=None, fscalemin=1e-5):
        """Reads star catalogue file as generated by the DRP. Coordinates are
        relative to target, (dx, dy) = (dRA, dDEC), in pixels. fscale is
        brightness relative to target.
        """
        with fits.open(starcatfile) as hdul:
            cat = hdul[1].data
            if maxrad is None:
                N = len(cat)
            else:
                N = np.searchsorted(cat['distance'], maxrad)
            if 'MAG_CHEOPS' in hdul[1].columns.names:
                fscale = 10**(-0.4*(cat['MAG_CHEOPS'][:N] - cat['MAG_CHEOPS'][0]))
            elif 'MAG_GAIA' in hdul[1].columns.names: # Name change in DRP v13
                fscale = 10**(-0.4*(cat['MAG_GAIA'][:N] - cat['MAG_GAIA'][0]))
            else:
                raise Exception(f'[read_starcat] Error: magnitude column not defined')

            dx = ((cat['RA'][0]-cat['RA'][:N]) * 
                   np.cos(np.deg2rad(cat['DEC'][0])) * 3600.0 / self.pxl_scl)
            dy = ((cat['DEC'][:N]-cat['DEC'][0]) * 3600.0 / self.pxl_scl)
            Teff = cat['T_EFF'][:N]
            gaiaID = cat['ID'][:N]

            sel = fscale > fscalemin
            return dx[sel], dy[sel], fscale[sel], Teff[sel], gaiaID[sel]

        
    def rotate_cat(self, rolldeg, maxrad=None):
        """Rotates the relative x and y positions of background
        stars according to submitted roll angle in degrees. Returns
        new relative rotated pixel coordinates (dx, dy).
        """
        if maxrad is not None:
            r2 = self.xpos**2+self.ypos**2
            Nmax = np.searchsorted(r2, maxrad**2)
            return rotate_position(self.xpos[:Nmax], self.ypos[:Nmax], rolldeg)
        return rotate_position(self.xpos, self.ypos, rolldeg)

    
    def rotate_entry(self, entry, rolldeg):
        """Rotates the relative x and y positions for a single
        background star according to submitted roll angle in degrees.
        Returns new relative rotated pixel coordinates (dx, dy).
        """
        return rotate_position(self.xpos[entry], self.ypos[entry], rolldeg)


    def bright_star_ids(self, limflux, outradius, inradius=0):
        """ Returns list of IDs of stars brighter
        than limflux and closer than outradius 
        (and outside of inradius) to target
        """
        dist = (self.xpos**2 + self.ypos**2)**.5
        ids = []
        for n in range(self.catsize):
            if self.fscale[n] < limflux:
                continue
            if dist[n] < inradius:
                continue
            if dist[n] > outradius:
                break
            ids.append(n)
        return ids


    def image(self, x0, y0, rolldeg, shape, skip=[0], limflux=0,
              single_id=None):
        """Produces image with background stars at defined roll angle.
        skip is a list of entries to be skipped. limflux is at what fractional
        flux of the target background stars should be ignored. The single_id is
        to select and draw an image of the selected star only.
        """        
        dx, dy = self.rotate_cat(rolldeg)
        
        xcoo = np.arange(shape[1]) - x0
        ycoo = np.arange(shape[0]) - y0
        ret_img = np.zeros(shape)
        
        if single_id is None:
            id_range = range(self.catsize)
        else:
            id_range = [single_id]
        
        for n in id_range:
            if n in skip: continue
            # Skip faint stars
            if self.fscale[n] < limflux: continue
            
            psf_rad = psf_radii(self.fscale[n])
            inds = find_area_inds(x0 + dx[n], y0 + dy[n], shape=shape, radius=psf_rad)
            if inds is None:
                continue
            else:
                i0, i1, j0, j1 = inds

            ddx = xcoo[i0:i1] - dx[n]
            ddy = ycoo[j0:j1] - dy[n]
            psf_fun = self.psfs[self.psf_ids[n]]
            psf_mat = psf_fun(ddx, ddy)
            if psf_mat.ndim == 1:
                psf_mat = np.reshape(psf_mat, (1, len(psf_mat)))            
            xmat,ymat = np.meshgrid(ddx,ddy)
            psf_mat[xmat**2+ymat**2 > psf_rad**2] = 0
            ret_img[j0:j1,i0:i1] += self.fscale[n] * psf_mat
        return ret_img


    def image_cat(self, x0, y0, rolldeg, blurdeg, maxrad, resolution=0.2):
        """Produces working catalog tuned for an image: transforms
        coordinates, produces relative coordinates for blur effect
        during exposure, and links appropriate PSFs to star entries.
        x0, y0 are centre of target in pixel coordinates
        """
        x, y = self.rotate_cat(rolldeg, maxrad=maxrad)
        Nstars = len(x)
        fscale = self.fscale[:Nstars].copy()
        dxs = []
        dys = []

        r = (x**2+y**2)**.5
        num_res = 0.5*np.deg2rad(blurdeg)*r/resolution
        for n in range(Nstars):
            N = 2*int(num_res[n])+1
            if N > 2:
                angles_deg = 0.5*blurdeg*np.linspace(-1, 1, N)
                rx, ry = rotate_position(x[n], y[n], angles_deg)
                dxs.append(rx-x[n])
                dys.append(ry-y[n])
            else:
                dxs.append(np.zeros(1))
                dys.append(np.zeros(1))

        return WorkCat(x=x+x0, y=y+y0, fscale=fscale,
                 dxs=dxs,
                 dys=dys)


    def smear(self, x0, y0, rolldeg, shape, limflux=1e-2):
        """Computes the smearing trail for all stars, including target.
        Returns a 1D array that can then be properly expanded to a 1D image.
        """
        im = self.image(x0, y0, rolldeg, shape=shape,
                        skip=[], limflux=limflux)
        return np.sum(im, axis=0)


def refine_bg_model(starids, data_frame, noise, mask, model, psf_norm,
                    work_cat, psf_ids, psfs, krn_scl=0.3, krn_rad=3):
    """Fit PSF for the stars in starids list to refine the correction.
    """
    xkern = np.linspace(-krn_rad, krn_rad, 2*krn_rad + 1)
    xkmat, ykmat = np.meshgrid(xkern, xkern)
    selk = (xkmat**2+ykmat**2) <= krn_rad**2
    kx = krn_scl*xkmat[selk]
    ky = krn_scl*ykmat[selk]

    for star_id in starids:
        psf_mod = psfs[psf_ids[star_id]]
        star_img = make_single_star_frame(data_frame.shape, work_cat, psf_mod, star_id, kx=kx, ky=ky)
        model -= psf_norm*star_img
        fit_frame = data_frame - model
#        psf_smear = partial(multi_psf, psf_mod=psf_mod,
#                            dxs=work_cat.dxs[star_id], dys=work_cat.dys[star_id])
        psf_smear = MultiPSF(psf_mod=psf_mod, dxs=work_cat.dxs[star_id],
                             dys=work_cat.dys[star_id])
        psf_rad = psf_radii(work_cat.fscale[star_id])
        dist = ((work_cat.x[star_id] - 0.5*data_frame.shape[1])**2 + 
                (work_cat.y[star_id] - 0.5*data_frame.shape[0])**2)**0.5
        if dist > 0.5*np.min(data_frame.shape):
            fitrad = psf_rad
        else:
            fitrad = 25
        
#        fitstar_img, _bg, kmat, _sc, _w = psf_fit([psf_smear], fit_frame, noise,
#                                                mask, xc=work_cat.x[star_id],
#                                                yc=work_cat.y[star_id], 
#                                                fitrad=fitrad, defrad=100,
#                                                krn_scl=krn_scl, krn_rad=krn_rad,
#                                                bg_fit=-1)

        fitstar_img, _bg, kmat, _sc, _w = psf_fit([psf_smear], fit_frame, noise,
                                                mask, xc=work_cat.x[star_id],
                                                yc=work_cat.y[star_id], 
                                                fitrad=fitrad, defrad=psf_rad,
                                                krn_scl=krn_scl, krn_rad=krn_rad,
                                                bg_fit=-1)
        model += fitstar_img
        knorm = np.sum(kmat)
        work_cat.fscale[star_id] *= knorm/(psf_norm*work_cat.fscale[star_id])
        work_cat.coeff[star_id] = kmat[selk] / knorm
    return work_cat



def make_bg_circ_mask(shape, work_cat, skip=[0], radius=20):
    """Produces frame of background stars as defined by work_cat.
    psf_ids are indices into the list psfs that contains various PSFs.
    kx and ky are offsets used by PSF fitting, only used by some
    star entries (those that then have coefficients defined in
    work_cat). Returns produced frame according to shape.
    """
    frame = np.zeros(shape)
    for n in range(work_cat.catsize):
        if n in skip:
            continue
        add_circle(frame,
                work_cat.x[n],
                work_cat.y[n],
                radius=radius)
    return frame == 0


def make_bg_psf_mask(shape, work_cat, psf_ids, psfs, skip=[0], kx=None, ky=None,
                 radius=25, level=0.1):
    """Produces frame of background stars as defined by work_cat.
    psf_ids are indices into the list psfs that contains various PSFs.
    kx and ky are offsets used by PSF fitting, only used by some
    star entries (those that then have coefficients defined in
    work_cat). Returns produced frame according to shape.
    """
    frame = np.zeros(shape)
    for n in range(work_cat.catsize):
        if n in skip:
            continue
        if kx is not None and work_cat.coeff[n] is not None:
            kmat = (work_cat.coeff[n], kx, ky)
        else:
            kmat = None
        add_psf_mask(frame,
                work_cat.x[n],
                work_cat.y[n],
                work_cat.dxs[n],
                work_cat.dys[n],
                psfs[psf_ids[n]],
                kmat=kmat,
                radius=radius,
                level=level)
    return frame == 0



def make_bg_frame(shape, work_cat, psf_ids, psfs, skip=[0], kx=None, ky=None):
    """Produces frame of background stars as defined by work_cat.
    psf_ids are indices into the list psfs that contains various PSFs.
    kx and ky are offsets used by PSF fitting, only used by some
    star entries (those that then have coefficients defined in
    work_cat). Returns produced frame according to shape.
    """
    radii = np.array(psf_radii(work_cat.fscale), dtype='int')
    frame = np.zeros(shape)
    for n in range(work_cat.catsize):
        if n in skip:
            continue
        if kx is not None and work_cat.coeff[n] is not None:
            kmat = (work_cat.coeff[n], kx, ky)
        else:
            kmat = None

        add_star(frame,
                 work_cat.fscale[n],
                 work_cat.x[n],
                 work_cat.y[n],
                 work_cat.dxs[n],
                 work_cat.dys[n],
                 psfs[psf_ids[n]],
                 kmat=kmat,
                 radius=radii[n])
    return frame


def make_single_star_frame(shape, work_cat, psf_mod, star_id, kx=None, ky=None):
    """Produces frame of background stars as defined by work_cat.
    psf_ids are indices into the list psfs that contains various PSFs.
    kx and ky are offsets used by PSF fitting, only used by some
    star entries (those that then have coefficients defined in
    work_cat). Returns produced frame according to shape.
    """
    radius = np.array(psf_radii(work_cat.fscale[star_id]), dtype='int')
    frame = np.zeros(shape)
    if kx is not None and work_cat.coeff[star_id] is not None:
        kmat = (work_cat.coeff[star_id], kx, ky)
    else:
        kmat = None

    add_star(frame,
            work_cat.fscale[star_id],
            work_cat.x[star_id],
            work_cat.y[star_id],
            work_cat.dxs[star_id],
            work_cat.dys[star_id],
            psf_mod,
            kmat=kmat,
            radius=radius)
    return frame


def psf_radii(fscales):
    """Computes suitable radii to use for PSFs in synthetic
    background image, considering the contamination. Returns
    a radius between 25 and 100 pixels, inclusive.
    """
#    return np.array(fscales*0+100, dtype='int') # DEBUG
    radii = 25 + 75*(fscales-1e-4)/(1e-1-1e-4)
    return np.array(np.ceil(np.minimum(np.maximum(radii, 25), 100)), dtype='int')


def add_star(frame, flux, x0, y0, dxs, dys, psf_mod, kmat=None, radius=30):
    x0i, y0i = int(x0), int(y0)
    x0f, y0f = x0-x0i, y0-y0i
    xi0,xi1,xj0,xj1 = find_inds(frame.shape[1], x0i, radius)
    yi0,yi1,yj0,yj1 = find_inds(frame.shape[0], y0i, radius)
    if kmat is None:
        star_frame = psf_image(x0f, y0f, dxs, dys, psf_mod, radius=radius)
    else:
        star_frame = psf_fit_image(kmat[0], x0f+kmat[1], y0f+kmat[2], dxs, dys, psf_mod, radius=radius)
    frame[yi0:yi1,xi0:xi1] += flux*star_frame[yj0:yj1, xj0:xj1]


def add_psf_mask(frame, x0, y0, dxs, dys, psf_mod, kmat=None, radius=30, level=0.1):
    x0i, y0i = int(x0), int(y0)
    x0f, y0f = x0-x0i, y0-y0i
    xi0,xi1,xj0,xj1 = find_inds(frame.shape[1], x0i, int(radius))
    yi0,yi1,yj0,yj1 = find_inds(frame.shape[0], y0i, int(radius))
    if kmat is None:
        star_frame = psf_image(x0f, y0f, dxs, dys, psf_mod, radius=radius)
    else:
        star_frame = psf_fit_image(kmat[0], x0f+kmat[1], y0f+kmat[2], dxs, dys, psf_mod, radius=radius)
    frame[yi0:yi1,xi0:xi1] += (star_frame[yj0:yj1, xj0:xj1] > level*np.max(star_frame))


def add_circle(frame, x0, y0, radius=20):
    im_rad = int(radius+1)
    x0i, y0i = int(x0), int(y0)
    x0f, y0f = x0-x0i, y0-y0i
    xi0,xi1,xj0,xj1 = find_inds(frame.shape[1], x0i, im_rad)
    yi0,yi1,yj0,yj1 = find_inds(frame.shape[0], y0i, im_rad)
    v = np.linspace(-im_rad, im_rad, 2*im_rad+1)
    X, Y = np.meshgrid(v-x0f, v-y0f)
    circ_frame = (X**2 + Y**2) <= radius**2
    frame[yi0:yi1,xi0:xi1] += circ_frame[yj0:yj1, xj0:xj1]



def find_inds(x_len, x, radius):
    """Finds indices i0, i1, j0, j1 such that a region b
    centered on x with radius, fits in region A of length
    x_len so that x_len[i0:i1] = b[j0:j1]. Returns indices
    zero if no overlap.
    """
    if (x < -radius) or (x > x_len + radius):
        return 0, 0, 0, 0
    if x > radius:
        i0 = x - radius
        j0 = 0
    else:
        i0 = 0
        j0 = radius - x
    if x + radius < x_len:
        i1 = x + radius + 1
        j1 = 2*radius + 1
    else:
        i1 = x_len
        j1 = radius - x + x_len
    return i0, i1, j0, j1


def psf_image(x0f, y0f, dxs, dys, psf_mod, radius=30):
    """Produce an image of a PSF blurred according to 
    dxs,dys relative coordinates. Use PSF model psf_mod
    over a 2*radius+1 square frame. x0f,y0f is the 
    fractional pixel offset from centre.
    """
    v = np.linspace(-radius, radius, int(2*radius+1))
    psf = MultiPSF(psf_mod, dxs, dys)
    return psf(v-x0f, v-y0f, circular=True)
 

def psf_fit_image(kf, kx, ky, dxs, dys, psf_mod, radius=30):
    """Produce an image of a PSF out of fit parameters
    kf, kx, ky where kf is coefficient for offest kx,ky.
    Use PSF model psf_mod over a 2*radius+1 square frame.
    """
    v = np.linspace(-radius, radius, 2*radius+1)
    psf_smear = MultiPSF(psf_mod, dxs, dys)
    ret = kf[0]*psf_smear(v-kx[0], v-ky[0], circular=False)
    for n in range(1, len(kf)):
        ret += kf[n]*psf_smear(v-kx[n], v-ky[n], circular=False)
    xx, yy = np.mgrid[-radius:(radius+1),-radius:(radius+1)]
    disc = xx**2+yy**2 <= radius**2
    return disc*ret


def make_multi_psf(psf_mod, dxs, dys):
    return partial(multi_psf, psf_mod=psf_mod, dxs=dxs, dys=dys)


def multi_psf(x, y, psf_mod, dxs, dys, circular=True):
    ret = psf_mod(x-dxs[0], y-dys[0], circular=circular)
    N = len(dxs)
    for n in range(1,N):
        ret += psf_mod(x-dxs[n], y-dys[n], circular=circular)
    return ret/N

class MultiPSF():
    def __init__(self, psf_mod, dxs, dys):
        self.psf_mod = psf_mod
        self.dxs = dxs
        self.dys = dys
        self.norm = psf_mod.norm

    def __call__(self, x, y, grid=True, circular=True):
        ret = self.psf_mod(x-self.dxs[0], y-self.dys[0],
                           grid=grid, circular=circular)
        N = len(self.dxs)
        for n in range(1,N):
            ret += self.psf_mod(x-self.dxs[n], y-self.dys[n],
                                grid=grid, circular=circular)
        return ret/N



def rotate_position(x, y, rolldeg):
    """Function that rotates coordinates according to
    roll angle (in degrees)
    """
    rollrad = np.deg2rad(rolldeg)
    cosa = np.cos(rollrad)
    sina = np.sin(rollrad)
    xroll = x * cosa + y * sina
    yroll = -x * sina + y * cosa
    return xroll, yroll


def derotate_position(xroll, yroll, rolldegs):
    """Function that de-rotates coordinates according to
    roll angle (in degrees)
    """
    return rotate_position(xroll, yroll, -rolldegs)


def find_area_inds(x, y, shape, radius):
    """Finds border indices for area in shape that is
    defined by a circle of radius at x,y (floating point)
    pixel coordinates
    """
    i = int(x)
    i0 = i - radius
    # Skip if further than radius outside image
    if i0 >= shape[1]: return None
    i1 = i + radius
    if i1 <= 0: return None
    i0 = max(i0, 0)
    i1 = min(i1, shape[1])
    j = int(y)
    j0 = j - radius
    if j0 >= shape[0]: return None
    j1 = j + radius
    if j1 <= 0: return None
    j0 = max(j0, 0)
    j1 = min(j1, shape[0])
    return (i0, i1, j0, j1)

