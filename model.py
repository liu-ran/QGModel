# Copyright (C) 2013,2014,2015 Jörn Callies
#
# This file is part of QGModel.
#
# QGModel is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# QGModel is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with QGModel.  If not, see <http://www.gnu.org/licenses/>.


import sys
import os
import pickle

import numpy as np
import pyfftw as fftw
import matplotlib.pyplot as plt
import matplotlib.colors as mcl


# Define blue colormap with nice blue.
cdict = {
    'red': ((0.0, 0.0, 0.0), (0.5, 0.216, 0.216), (1.0, 1.0, 1.0)), \
    'green': ((0.0, 0.0, 0.0), (0.5, 0.494, 0.494), (1.0, 1.0, 1.0)), \
    'blue': ((0.0, 0.0, 0.0), (0.5, 0.722, 0.722), (1.0, 1.0, 1.0))}
cm_blues = mcl.LinearSegmentedColormap('cm_blues', cdict, 256)


class Model:

    """General QG model

    This is the skeleton of a QG model that consists of a number of conserved
    quantities that are advected horizontally.  Implementations of this model
    need to specify the number of conserved quantities (nz) and supply an
    inversion relation that yield the streamfuncion given the conserved quan-
    tities.  The model geometry is doubly periodic in the perturbations; mean
    flow and gradients in the conserved quantities can be prescribed.
    """

    def __init__(self, L, n, nz, dt):
        self.L = L              # domain size
        self.n = n              # number of Fourier modes per direction
        self.nz = nz            # number of levels
        self.dt = dt            # time step
        self.u = np.zeros(nz)   # mean zonal velocities
        self.v = np.zeros(nz)   # mean meridional velocities
        self.qx = np.zeros(nz)  # mean zonal PV gradient
        self.qy = np.zeros(nz)  # mean meridional PV gradient
        self.diffexp = 2        # exponent of diffusion operator
        self.hypodiff = 0.      # hypodiffusion coefficient
        self.threads = 1        # number of threads for FFT
        self.grid()             # set up grid
        self.time = 0.          # initialize time


    def grid(self):
        """Set up spectral and physical grid."""
        # spectral grid
        k = abs(np.fft.fftfreq(self.n, d=self.L/(2*np.pi*self.n))[:self.n/2+1])
        l = np.fft.fftfreq(self.n, d=self.L/(2*np.pi*self.n))
        self.k, self.l = np.meshgrid(k, l)
        # physical grid
        x = np.arange(self.n) * self.L / self.n
        y = np.arange(self.n) * self.L / self.n
        self.x, self.y = np.meshgrid(x, y)


    def initq(self, qp):
        """Transform qp to spectral space and initialize q."""
        self.q = fftw.interfaces.numpy_fft.rfft2(qp, threads=self.threads)
        self.q[:,0,0] = 0.  # ensure zero mean


    def timestep(self):
        """Perform time step."""
        self.advection()
        self.diffusion()
        self.time += self.dt

    def advection(self):
        """Perform RK4 step for advective terms (linear and nonlinear)."""
        q1 = self.dt * self.advrhs(self.q)
        q2 = self.dt * self.advrhs(self.q + q1/2)
        q3 = self.dt * self.advrhs(self.q + q2/2)
        q4 = self.dt * self.advrhs(self.q + q3)
        self.q += (q1 + 2*q2 + 2*q3 + q4) / 6

    def diffusion(self):
        """Perform impolicit (hyper- and hypo-) diffusion step."""
        k2 = self.k**2 + self.l**2
        k2[0,0] = 1.  # Prevent div. by zero.
        self.q *= np.exp(-self.nu * k2**(self.diffexp/2.) * self.dt)
        if self.hypodiff > 0:
            self.q *= np.exp(-self.hypodiff / k2 * self.dt)

    def advrhs(self, q):
        """Calculate advective terms on RHS of PV equation.
        
        Calculate mean-eddy and eddy-eddy advection terms:
            u q'_x + v q'_y + u' q_x + v' q_y + J(p', q')
        """
        p = self.invert(q)
        rhs = np.empty([self.nz, self.n, self.n/2 + 1], dtype=complex)
        for i in range(self.nz):
            rhs[i,:,:] = \
                - 1j * (self.k*self.u[i] + self.l*self.v[i]) * q[i,:,:] \
                - 1j * (self.k*self.qy[i] - self.l*self.qx[i]) * p[i,:,:] \
                - self.jacobian(p[i,:,:], q[i,:,:])
        return rhs

    def jacobian(self, A, B):
        """Calculate Jacobian A_x B_y - A_y B_x.
        
        Transform Ax, Ay, Bx, By to physical space, perform multi-
        plication and subtraction, and transform back to spectral space.
        To avoid aliasing, apply 3/2 padding.
        """
        Axp = self.ifft_pad(1j * self.k * A)
        Ayp = self.ifft_pad(1j * self.l * A)
        Bxp = self.ifft_pad(1j * self.k * B)
        Byp = self.ifft_pad(1j * self.l * B)
        return self.fft_truncate(Axp * Byp - Ayp * Bxp)

    def fft_truncate(self, up):
        """Perform forward FFT on physical field up and truncate (3/2 rule)."""
        us = fftw.interfaces.numpy_fft.rfft2(up, threads=self.threads)
        u = np.zeros((self.n, self.n/2 + 1), dtype=complex)
        u[: self.n/2, :] = us[: self.n/2, : self.n/2 + 1]
        u[self.n/2 :, :] = us[self.n : 3*self.n/2, : self.n/2 + 1]
        return u/2.25  # accounting for normalization

    def ifft_pad(self, u):
        """Pad spectral field u (3/2 rule) and perform inverse FFT."""
        us = np.zeros((3*self.n/2, 3*self.n/4 + 1), dtype=complex)
        us[: self.n/2, : self.n/2 + 1] = u[: self.n/2, :]
        us[self.n : 3*self.n/2, : self.n/2 + 1] = u[self.n/2 :, :]
        return fftw.interfaces.numpy_fft.irfft2(2.25*us, threads=self.threads)

    def doubleres(self):
        """Double the resolution, interpolate fields."""
        self.n *= 2
        # Pad spectral field.
        qs = np.zeros((self.nz, self.n, self.n/2 + 1), dtype=complex)
        qs[:, : self.n/4, : self.n/4 + 1] = self.q[:, : self.n/4, :]
        qs[:, 3*self.n/4 : self.n, : self.n/4 + 1] = self.q[:, self.n/4 :, :]
        # Account for normalization.
        self.q = 4 * qs
        # Update grid.
        self.grid()

    def screenlog(self):
        """Print model state info on screen."""
        # time (in seconds)
        sys.stdout.write(' {:15.0f}'.format(self.time))
        # enstrophy for each layer
        for i in range(self.nz):
            sys.stdout.write(' {:5e}'.format(np.sum(np.abs(self.q[i,:,:])**2)))
        sys.stdout.write('\n')

    def snapshot(self, name):
        """Save snapshots of total q (mean added) for each level."""
        # Check if directory exists.
        if not os.path.isdir(name + '/snapshots'):
            os.makedirs(name + '/snapshots')
        # Iterate over layers.
        for i in range(self.nz):
            # Trasform to physical space.
            qp = fftw.interfaces.numpy_fft.irfft2(
                    self.q[i,:,:], threads=self.threads)
            # Add background gradients.
            qp += self.qx[i] * (self.x - self.L / 2)
            qp += self.qy[i] * (self.y - self.L / 2)
            # Set range of colorbar.
            m = max(abs(self.qx[i] * self.L), abs(self.qy[i] * self.L))
            # Save image.
            plt.imsave(
                name + '/snapshots/{:03d}_{:015.0f}.png'.format(i, self.time),
                qp, origin='lower', vmin=-m, vmax=m, cmap=cm_blues)

    def save(self, name):
        """Save model state."""
        # Check if directory exists.
        if not os.path.isdir(name + '/data'):
            os.makedirs(name + '/data')
        # Save.
        f = open(name + '/data/{:015.0f}'.format(self.time), 'w')
        pickle.dump(self, f)


def load(name, time):
    """Load model state."""
    f = open(name + '/data/{:015.0f}'.format(time), 'r')
    return pickle.load(f)


class TwoDim(Model):

    """Two dimensional model

    This implements a two-dimensional model that consists of vorticity conser-
    vation.  The inversion relation is simply a Poisson equation.
    """

    def __init__(self, L, n, dt):
        Model.__init__(self, L, n, 1, dt)

    def invert(self, q):
        """2D inversion: q = - (k^2 + l^2) p."""
        k2 = self.k**2 + self.l**2
        k2[0,0] = 1.  # prevent div. by zero
        return - q / k2


class Eady(Model):

    """Eady model

    This implements an Eady model that consists of surface and bottom buoyancy
    conservation and implicit interior dynamics determined by zero PV there.
    The conserved quantities here are PV-like:
      q[0] = - f b(0) / N^2,
      q[1] = + f b(-H) / N^2.
    """

    def __init__(self, L, n, dt):
        Model.__init__(self, L, n, 2, dt)

    def initmean(self, f, N, H, Sx, Sy):
        """Initialize the mean state of the model."""
        self.f = f  # Coriolis parameter
        self.N = N  # buoyancy frequency
        self.H = H  # depth
        # mean flow
        self.u = np.array([0, - Sx * H])
        self.v = np.array([0, - Sy * H])
        # mean PV gradients
        self.qx = np.array([f**2 * Sy / N**2, -f**2 * Sy / N**2])
        self.qy = np.array([f**2 * Sx / N**2, -f**2 * Sx / N**2])

    def invert(self, q):
        """Eady inversion."""
        p = np.zeros([self.nz, self.n, self.n/2 + 1], dtype=complex)
        kh = np.hypot(self.k, self.l)
        kh[0,0] = 1.  # prevent div. by zero
        mu = self.N * kh * self.H / self.f
        p[0,:,:] = q[0,:,:]/np.tanh(mu) + q[1,:,:]/np.sinh(mu)
        p[1,:,:] = q[0,:,:]/np.sinh(mu) + q[1,:,:]/np.tanh(mu)
        return - p * self.N / (self.f * kh)


class FloatingEady(Model):

    """Floating Eady model

    This implements a "floating" Eady model that consists of a layer of
    constant PV coupled to an infinitely deep layer below that also has
    constant PV (see Callies, Flierl, Ferrari, Fox-Kemper, 2015).  The model
    consists of two conserved PV-like quantities at the surface and the inter-
    face between the layers:
      q[0] = - f b(0) / N[0]^2,
      q[1] = + f [b^+(-H) / N[0]^2 - b^-(-H) / N[1]^2,
    where N[0] and N[1] are the buoyancy frequencies of the Eady and deep
    layers, respectively.
    """

    def __init__(self, L, n, dt):
        Model.__init__(self, L, n, 2, dt)

    def initmean(self, f, N, H, Sx, Sy):
        """Initialize the mean state of the model."""
        self.f = f            # Coriolis parameter
        self.N = np.array(N)  # buoyancy frequencies of the two layers
        self.H = H            # depth of upper layer
        # mean flow
        self.u = np.array([0, - Sx[0] * H])
        self.v = np.array([0, - Sy[0] * H])
        # mean PV gradients
        self.qx = np.array([
            f**2 * Sy[0] / N[0]**2,
            -f**2 * (Sy[0] / N[0]**2 - Sy[1] / N[1]**2)])
        self.qy = np.array([
            f**2 * Sx[0] / N[0]**2,
            -f**2 * (Sx[0] / N[0]**2 - Sx[1] / N[1]**2)])

    def invert(self, q):
        """Floating Eady inversion."""
        p = np.zeros([self.nz, self.n, self.n/2 + 1], dtype=complex)
        kh = np.hypot(self.k, self.l)
        kh[0,0] = 1.  # prevent div. by zero
        mu = self.N[0] * kh * self.H / self.f
        p[0,:,:] = (self.N[0]+self.N[1]) * q[0,:,:] / np.tanh(mu) \
            + self.N[1] * q[1,:,:] / np.sinh(mu)
        p[1,:,:] = self.N[1] * q[0,:,:] / np.sinh(mu) \
            + self.N[1] * q[1,:,:] / np.tanh(mu)
        return - p * self.N[0] \
            / (self.f * kh * (self.N[0] / np.tanh(mu) + self.N[1]))


class TwoEady(Model):

    """Two-Eady model

    This implements a two-Eady model that consists of two layers of
    constant PV coupled at a deformable interface (see Callies, Flierl,
    Ferrari, Fox-Kemper, 2015).  The model consists of three conserved PV-like
    quantities at the surface, the interface between the layers, and the
    bottom:
      q[0] = - f b(0) / N[0]^2,
      q[1] = + f [b^+(-H[0]) / N[0]^2 - b^-(-H[0]) / N[0]^2,
      q[0] = + f b(-H[0]-H[1]) / N[1]^2,
    where N[0] and N[1] are the buoyancy frequencies of the two layers and H[0]
    and H[1] are their depths.
    """

    def __init__(self, L, n, dt):
        Model.__init__(self, L, n, 3, dt)

    def initmean(self, f, N, H, Sx, Sy):
        """Initialize the mean state of the model."""
        self.f = f            # Coriolis parameter
        self.N = np.array(N)  # buoyancy frequencies of the two layers
        self.H = np.array(H)  # depths of the two layers
        # mean flow
        self.u = np.array([0, - Sx[0] * H[0], - Sx[0] * H[0] - Sx[1] * H[1]])
        self.v = np.array([0, - Sy[0] * H[0], - Sy[0] * H[0] - Sy[1] * H[1]])
        # mean PV gradients
        self.qx = np.array([
            f**2 * Sy[0] / N[0]**2,
            -f**2 * (Sy[0] / N[0]**2 - Sy[1] / N[1]**2),
            -f**2 * Sy[1] / N[1]**2])
        self.qy = np.array([
            f**2 * Sx[0] / N[0]**2,
            -f**2 * (Sx[0] / N[0]**2 - Sx[1] / N[1]**2),
            -f**2 * Sx[1] / N[1]**2])

    def invert(self, q):
        """Two-Eady inversion."""
        p = np.zeros([self.nz, self.n, self.n/2 + 1], dtype=complex)
        kh = np.hypot(self.k, self.l)
        kh[0,0] = 1.  # prevent div. by zero
        mu = np.zeros([self.nz, self.n, self.n/2 + 1])
        mu[0,:,:] = self.N[0] * kh * self.H[0] / self.f
        mu[1,:,:] = self.N[1] * kh * self.H[1] / self.f
        p[0,:,:] = \
            (self.N[0]**2 + self.N[0] * self.N[1] / np.tanh(mu[0,:,:]) 
            / np.tanh(mu[1,:,:])) * q[0,:,:] \
            + self.N[0] * self.N[1] / np.sinh(mu[0,:,:]) / np.tanh(mu[1,:,:]) \
                * q[1,:,:] \
            + self.N[0] * self.N[1] / np.sinh(mu[0,:,:]) / np.sinh(mu[1,:,:]) \
                * q[2,:,:]
        p[1,:,:] = \
            self.N[0] * self.N[1] / np.sinh(mu[0,:,:]) / np.tanh(mu[1,:,:]) \
                * q[0,:,:] \
            + self.N[0] * self.N[1] / np.tanh(mu[0,:,:]) / np.tanh(mu[1,:,:]) \
                * q[1,:,:] \
            + self.N[0] * self.N[1] / np.tanh(mu[0,:,:]) / np.sinh(mu[1,:,:]) \
                * q[2,:,:]
        p[2,:,:] = \
            self.N[0] * self.N[1] / np.sinh(mu[0,:,:]) / np.sinh(mu[1,:,:]) \
                * q[0,:,:] \
            + self.N[0] * self.N[1] / np.tanh(mu[0,:,:]) / np.sinh(mu[1,:,:]) \
                * q[1,:,:] \
            + (self.N[1]**2 + self.N[0] * self.N[1] / np.tanh(mu[0,:,:]) \
                / np.tanh(mu[1,:,:])) * q[2,:,:]
        return - p / (self.f * kh * (self.N[0] / np.tanh(mu[0,:,:])
            + self.N[1] / np.tanh(mu[1,:,:])))