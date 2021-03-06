'''
by Evan Mayer

Library for data collection functions on an rtl-sdr based radio telescope.
'''

import numpy as np
from scipy.signal import welch, get_window
import sys
import time

from rtlsdr import RtlSdr


def run_total_power_int(num_samp, gain, rate, fc, t_int):
    '''
    Implement a total-power radiometer. Raw, uncalibrated power values.

    Inputs:
    num_samp:   Number of elements to sample from the SDR IQ timeseries per call
    gain:       Requested SDR gain (dB)
    rate:       SDR sample rate, intrinsically tied to bandwidth in SDRs (Hz)
    fc:         Bandpass center frequency (Hz)
    t_int:      Total integration time (s)

    Returns:
    p_tot:   Time-averaged power in the signal from the sdr, in 
             uncalibrated units
    '''
    import rtlsdr.helpers as helpers

    # Start the RtlSdr instance
    print('Initializing rtl-sdr with pyrtlsdr:')
    sdr = RtlSdr()

    try:
        sdr.rs = rate
        sdr.fc = fc
        sdr.gain = gain
        print('  sample rate: {} MHz'.format(sdr.rs / 1e6))
        print('  center frequency {} MHz'.format(sdr.fc / 1e6))
        print('  gain: {} dB'.format(sdr.gain))
        print('  num samples per call: {}'.format(num_samp))
        print('  requested integration time: {}s'.format(t_int))
        # For Nyquist sampling of the passband dv over an integration time
        # tau, we must collect N = 2 * dv * tau real samples.
        # https://www.cv.nrao.edu/~sransom/web/A1.html#S3
        # Because the SDR collects complex samples at a rate rs = dv, we can
        # Nyquist sample a signal of band-limited noise dv with only rs * tau
        # complex samples.
        # The phase content of IQ samples allows the bandlimited signal to be
        # Nyquist sampled at a data rate of rs = dv complex samples per second
        # rather than the 2* dv required of real samples.
        N = int(sdr.rs * t_int)
        print('  => num samples to collect: {}'.format(N))
        print('  => est. num of calls: {}'.format(int(N / num_samp)))

        global p_tot
        global cnt
        p_tot = 0.0
        cnt = 0

        # Set the baseline time
        start_time = time.time()
        print('Integration began at {}'.format(time.strftime('%a, %d %b %Y %H:%M:%S', time.localtime(start_time))))

        # Time integration loop
        @helpers.limit_calls(N / num_samp)
        def p_tot_callback(iq, context):
            # The below is a total power measurement equivalent to summing
            # P = V^2 / R = (sqrt(I^2 + Q^2))^2 = (I^2 + Q^2)
            global p_tot 
            p_tot += np.sum(np.real(iq * np.conj(iq)))
            global cnt 
            cnt += 1
        sdr.read_samples_async(p_tot_callback, num_samples=num_samp)
        
        end_time = time.time()
        print('Integration ended at {} after {} seconds.'.format(time.strftime('%a, %d %b %Y %H:%M:%S'), end_time-start_time))
        print('{} calls were made to SDR.'.format(cnt))
        print('{} samples were measured at {} MHz'.format(cnt * num_samp, fc / 1e6))
        print('for an effective integration time of {:.2f}s'.format( (num_samp * cnt) / rate))

        # Compute the average power value based on the number of measurements 
        # we actually did
        p_avg = p_tot / (num_samp * cnt)

        # nice and tidy
        sdr.close()

    except OSError as err:
        print("OS error: {0}".format(err))
        raise(err)
    except:
        print('Unexpected error:', sys.exc_info()[0])
        raise
    finally:
        sdr.close()
    
    return p_avg


def run_spectrum_int( num_samp, nbins, gain, rate, fc, t_int ):
    '''
    Inputs:
    num_samp: Number of elements to sample from the SDR IQ per call;
              use powers of 2
    nbins:    Number of frequency bins in the resulting power spectrum; powers
              of 2 are most efficient, and smaller numbers are faster on CPU.
    gain:     Requested SDR gain (dB)
    rate:     SDR sample rate, intrinsically tied to bandwidth in SDRs (Hz)
    fc:       Base center frequency (Hz)
    t_int:    Total effective integration time (s)

    Returns:
    freqs:       Frequencies of the resulting spectrum, centered at fc (Hz), 
                 numpy array
    p_avg_db_hz: Power spectral density (dB/Hz) numpy array
    '''
    # Force a choice of window to allow converting to PSD after averaging
    # power spectra
    WINDOW = 'hann'
    # Force a default nperseg for welch() because we need to get a window
    # of this size later. Use the scipy default 256, but enforce scipy 
    # conditions on nbins vs. nperseg when nbins gets small. 
    if nbins < 256:
        nperseg = nbins
    else:
        nperseg = 256

    print('Initializing rtl-sdr with pyrtlsdr:')
    sdr = RtlSdr()

    try:
        sdr.rs = rate # Rate of Sampling (intrinsically tied to bandwidth with SDR dongles)
        sdr.fc = fc
        sdr.gain = gain
        print('  sample rate: %0.6f MHz' % (sdr.rs / 1e6))
        print('  center frequency %0.6f MHz' % (sdr.fc / 1e6))
        print('  gain: %d dB' % sdr.gain)
        print('  num samples per call: {}'.format(num_samp))
        print('  PSD binning: {} bins'.format(nbins))
        print('  requested integration time: {}s'.format(t_int))
        N = int(sdr.rs * t_int)
        num_loops = int(N / num_samp) + 1
        print('  => num samples to collect: {}'.format(N))
        print('  => est. num of calls: {}'.format(num_loops - 1))

        # Set up arrays to store power spectrum calculated from I-Q samples
        freqs = np.zeros(nbins)
        p_xx_tot = np.zeros(nbins)
        cnt = 0

        # Set the baseline time
        start_time = time.time()
        print('Integration began at {}'.format(time.strftime('%a, %d %b %Y %H:%M:%S', time.localtime(start_time))))
        # Estimate the power spectrum by Bartlett's method.
        # Following https://en.wikipedia.org/wiki/Bartlett%27s_method: 
        # Use scipy.signal.welch to compute one spectrum for each timeseries
        # of samples from a call to the SDR.
        # The scipy.signal.welch() method with noverlap=0 is equivalent to 
        # Bartlett's method, which estimates the spectral content of a time-
        # series by splitting our num_samp array into K segments of length
        # nperseg and averaging the K periodograms.
        # The idea here is to average many calls to welch() across the
        # requested integration time; this means we can call welch() on each 
        # set of samples from the SDR, accumulate the binned power estimates,
        # and average later by the number of spectra taken to reduce the 
        # noise while still following Barlett's method, and without keeping 
        # huge arrays of iq samples around in RAM.
        
        # Time integration loop
        for cnt in range(num_loops):
            iq = sdr.read_samples(num_samp)
            
            freqs, p_xx = welch(iq, fs=rate, nperseg=nperseg, nfft=nbins, noverlap=0, scaling='spectrum', window=WINDOW, detrend=False, return_onesided=False)
            p_xx_tot += p_xx
        
        end_time = time.time()
        print('Integration ended at {} after {} seconds.'.format(time.strftime('%a, %d %b %Y %H:%M:%S'), end_time-start_time))
        print('{} spectra were measured at {}.'.format(cnt, fc))
        print('for an effective integration time of {:.2f}s'.format(num_samp * cnt / rate))

        # Unfortunately, welch() with return_onesided=False does a sloppy job
        # of returning the arrays in what we'd consider the "right" order,
        # so we have to swap the first and last halves to avoid an artifact
        # in the plot.
        half_len = len(freqs) // 2

        freqs = np.fft.fftshift(freqs)
        p_xx_tot = np.fft.fftshift(p_xx_tot)

        # Compute the average power spectrum based on the number of spectra read
        p_avg = p_xx_tot / cnt

        # Convert to power spectral density
        # A great resource that helped me understand the difference:
        # https://community.sw.siemens.com/s/article/what-is-a-power-spectral-density-psd
        # We could just divide by the bandwidth, but welch() applies a
        # windowing correction to the spectrum, and does it differently to
        # power spectra and PSDs. We multiply by the power spectrum correction 
        # factor to remove it and divide by the PSD correction to apply it 
        # instead. Then divide by the bandwidth to get the power per unit 
        # frequency.
        # See the scipy docs for _spectral_helper().
        win = get_window(WINDOW, nperseg)
        p_avg_hz = p_avg * ((win.sum()**2) / (win*win).sum()) / rate

        p_avg_db_hz = 10. * np.log10(p_avg_hz)

        # Shift frequency spectra back to the intended range
        freqs = freqs + fc

        # nice and tidy
        sdr.close()

    except OSError as err:
        print("OS error: {0}".format(err))
        raise(err)
    except:
        print('Unexpected error:', sys.exc_info()[0])
        raise
    finally:
        sdr.close()

    return freqs, p_avg_db_hz


def run_fswitch_int( num_samp, nbins, gain, rate, fc, fthrow, t_int, fswitch=10):
    '''
    Note: Because a significant time penalty is introduced for each retuning,
          a maximum frequency switching rate of 10 Hz is adopted to help 
          reduce the fraction of observation time spent retuning the SDR
          for a given effective integration time.
          As a consequence, the minimum integration time is 2*(1/fswitch)
          to ensure the user gets at least one spectrum taken on each
          frequency of interest.
    Inputs:
    num_samp: Number of elements to sample from the SDR IQ timeseries: powers of 2 are most efficient
    nbins:    Number of frequency bins in the resulting power spectrum; powers
              of 2 are most efficient, and smaller numbers are faster on CPU.
    gain:     Requested SDR gain (dB)
    rate:     SDR sample rate, intrinsically tied to bandwidth in SDRs (Hz)
    fc:       Base center frequency (Hz)
    fthrow:   Alternate frequency (Hz)
    t_int:    Total effective integration time (s)
    Kwargs:
    fswitch:  Frequency of switching between fc and fthrow (Hz)

    Returns:
    freqs_fold: Frequencies of the spectrum resulting from folding according to the folding method implemented in the f_throw_fold (post_process module)
    p_fold:     Folded frequency-switched power, centered at fc,(uncalibrated V^2) numpy array.
    '''
    from .post_process import f_throw_fold 
    import rtlsdr.helpers as helpers

    # Check inputs:
    assert t_int >= 2.0 * (1.0/fswitch), '''At t_int={} s, frequency switching at fswitch={} Hz means the switching period is longer than integration time. Please choose a longer integration time or shorter switching frequency to ensure enough integration time to dwell on each frequency.'''.format(t_int, fswitch)

    if fswitch > 10:
        print('''Warning: high frequency switching values mean more SDR retunings. A greater fraction of observation time will be spent retuning the SDR, resulting in longer wait times to reach the requested effective integration time.''')

    print('Initializing rtl-sdr with pyrtlsdr:')
    sdr = RtlSdr()

    try:
        sdr.rs = rate # Rate of Sampling (intrinsically tied to bandwidth with SDR dongles)
        sdr.fc = fc
        sdr.gain = gain
        print('  sample rate: %0.6f MHz' % (sdr.rs/1e6))
        print('  center frequency %0.6f MHz' % (sdr.fc/1e6))
        print('  gain: %d dB' % sdr.gain)
        print('  num samples per call: {}'.format(num_samp))
        print('  requested integration time: {}s'.format(t_int))
        
        # Total number of samples to collect
        N = int(sdr.rs * t_int)
        # Number of samples on each frequency dwell
        N_dwell = int(sdr.rs * (1.0 / fswitch))
        # Number of calls to SDR on each frequency
        num_loops = N_dwell//num_samp
        # Number of dwells on each frequency
        num_dwells = N//N_dwell
        print('  => num samples to collect: {}'.format(N))
        print('  => est. num of calls: {}'.format(N//num_samp))
        print('  => num samples on each dwell: {}'.format(N_dwell))
        print('  => est. num of calls on each dwell: {}'.format(num_loops))
        print('  => num dwells total: {}'.format(num_dwells))

        # Set up arrays to store power spectrum calculated from I-Q samples
        
        freqs_on = np.zeros(nbins)
        freqs_off = np.zeros(nbins)
        p_xx_on = np.zeros(nbins)
        p_xx_off = np.zeros(nbins)
        cnt = 0

        # Set the baseline time
        start_time = time.time()
        print('Integration began at {}'.format(time.strftime('%a, %d %b %Y %H:%M:%S', time.localtime(start_time))))

        # Swap between the two specified frequencies, integrating signal.
        # Time integration loop
        for i in range(num_dwells):
            tick = (i%2 == 0)
            if tick:
                sdr.fc = fc
            else:
                sdr.fc = fthrow
            for j in range(num_loops):
                iq = sdr.read_samples(num_samp)

                if tick:
                    freqs_on, p_xx = welch(iq, fs=rate, nperseg=nbins, noverlap=0, scaling='spectrum', detrend=False, return_onesided=False)
                    p_xx_on += p_xx
                else:
                    freqs_off, p_xx = welch(iq, fs=rate, nperseg=nbins, noverlap=0, scaling='spectrum', detrend=False, return_onesided=False)
                    p_xx_off += p_xx
                cnt += 1
        
        end_time = time.time()
        print('Integration ended at {} after {} seconds.'.format(time.strftime('%a, %d %b %Y %H:%M:%S'), end_time-start_time))
        print('{} spectra were measured, split between {} and {}.'.format(cnt, fc, fthrow))
        print('for an effective integration time of {:.2f}s'.format(num_samp * cnt / rate))

        half_len = len(freqs_on)//2
        freqs_on = np.fft.fftshift(freqs_on)
        freqs_off = np.fft.fftshift(freqs_off)

        p_xx_on = np.fft.fftshift(p_xx_on)
        p_xx_off = np.fft.fftshift(p_xx_off)

        # Compute the average power spectrum based on the number of spectra read
        p_avg_on  = p_xx_on  / cnt
        p_avg_off = p_xx_off / cnt
        # Shift frequency spectra back to the intended range
        freqs_on = freqs_on + fc
        freqs_off = freqs_off + fthrow

        # Fold switched power spectra
        freqs_fold, p_fold = f_throw_fold(freqs_on, freqs_off, p_avg_on, p_avg_off)

        # nice and tidy
        sdr.close()

    except OSError as err:
        print("OS error: {0}".format(err))
        raise(err)
    except:
        print('Unexpected error:', sys.exc_info()[0])
        raise
    finally:
        sdr.close()

    return freqs_fold, p_fold


def save_spectrum(filename, freqs, p_xx):
    '''
    Save the results of integration to a file.
    '''
    header='\n\n\n\n\n'
    np.savetxt(filename, np.column_stack((freqs, p_xx)), delimiter=' ', header=header)
    print('Results were written to {}.'.format(filename))

    return

